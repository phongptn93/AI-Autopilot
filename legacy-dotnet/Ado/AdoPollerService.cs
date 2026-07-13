using System.Collections.Concurrent;
using AdoAutopilot.Api;
using AdoAutopilot.Data;
using AdoAutopilot.Execution;
using AdoAutopilot.Metrics;
using AdoAutopilot.Models;
using AdoAutopilot.Routing;
using AdoAutopilot.Scheduling;
using AdoAutopilot.Security;
using Microsoft.Extensions.Options;

namespace AdoAutopilot.Ado;

/// <summary>
/// Background service that polls ADO for work items assigned to the bot.
/// </summary>
public class AdoPollerService : BackgroundService
{
    private readonly AdoClient _ado;
    private readonly TaskRouter _router;
    private readonly ClaudeExecutor _executor;
    private readonly AdoNotifier _notifier;
    private readonly AutopilotConfig _config;
    private readonly ILogger<AdoPollerService> _logger;
    private readonly RetryPolicy _retryPolicy;
    private readonly ExecutionRepository _execRepo;
    private readonly ScheduleGuard _schedule;
    private readonly RequirementDecomposer _decomposer;
    private readonly RbacPolicy _rbac;

    // Track processed items to avoid re-processing
    private readonly ConcurrentDictionary<int, DateTime> _processed = new();
    private readonly SemaphoreSlim _concurrencyGate;

    public AdoPollerService(
        AdoClient ado,
        TaskRouter router,
        ClaudeExecutor executor,
        AdoNotifier notifier,
        IOptions<AutopilotConfig> config,
        ILogger<AdoPollerService> logger,
        RetryPolicy retryPolicy,
        ExecutionRepository execRepo,
        ScheduleGuard schedule,
        RequirementDecomposer decomposer,
        RbacPolicy rbac)
    {
        _ado = ado;
        _router = router;
        _executor = executor;
        _notifier = notifier;
        _config = config.Value;
        _logger = logger;
        _retryPolicy = retryPolicy;
        _execRepo = execRepo;
        _schedule = schedule;
        _decomposer = decomposer;
        _rbac = rbac;
        _concurrencyGate = new SemaphoreSlim(_config.MaxConcurrent);
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        _logger.LogInformation("╔══════════════════════════════════════════╗");
        _logger.LogInformation("║       ADO Autopilot — Started            ║");
        _logger.LogInformation("║  Org: {Org}", _config.AdoOrganization);
        _logger.LogInformation("║  Project: {Project}", _config.AdoProject);
        _logger.LogInformation("║  Trigger tag: '{Tag}'", _config.TriggerTag);
        _logger.LogInformation("║  Poll interval: {Interval}s", _config.PollIntervalSeconds);
        _logger.LogInformation("║  Dry-run: {DryRun}", _config.DryRun);
        _logger.LogInformation("╚══════════════════════════════════════════╝");

        var hasAuth = !string.IsNullOrEmpty(_config.AdoPat) ||
                     !string.IsNullOrEmpty(_config.OAuthAppId);
        if (!hasAuth)
        {
            _logger.LogWarning("⚠ No auth configured — running in offline mode (no ADO polling)");
            _logger.LogWarning("  Set either Autopilot:AdoPat (PAT) or Autopilot:OAuthAppId + OAuthAppSecret (OAuth)");
            await Task.Delay(Timeout.Infinite, ct);
            return;
        }

        if (!string.IsNullOrEmpty(_config.OAuthAppId))
            _logger.LogInformation("║  Auth: OAuth (browser redirect)");

        while (!ct.IsCancellationRequested)
        {
            try
            {
                await PollAndProcessAsync(ct);
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                _logger.LogError(ex, "Poll cycle failed");
            }

            // Cleanup old processed entries (older than 1 hour)
            var cutoff = DateTime.UtcNow.AddHours(-1);
            foreach (var kv in _processed.Where(kv => kv.Value < cutoff))
                _processed.TryRemove(kv.Key, out _);

            await Task.Delay(TimeSpan.FromSeconds(_config.PollIntervalSeconds), ct);
        }
    }

    private async Task PollAndProcessAsync(CancellationToken ct)
    {
        // Check schedule window
        if (!_schedule.IsWithinWindow())
        {
            _logger.LogDebug("Outside schedule window — skipping execution");
            return;
        }

        // Process webhook-queued items first
        var webhookIds = new List<int>();
        while (WebhookController.TryDequeue(out var wid)) webhookIds.Add(wid);
        if (webhookIds.Count > 0)
        {
            _logger.LogInformation("Processing {Count} webhook-triggered items", webhookIds.Count);
            var webhookItems = await _ado.GetWorkItemsByIdsAsync(webhookIds, ct);
            foreach (var wi in webhookItems)
            {
                if (!_processed.ContainsKey(wi.Id) && !_retryPolicy.IsExhausted(wi.Id))
                    _ = ProcessWorkItemAsync(wi, ct);
            }
        }

        _logger.LogDebug("Polling ADO for pending work items...");
        var items = await _ado.GetPendingWorkItemsAsync(ct);

        // Filter out already-processed, items with "done"/"review" tag, items in backoff, and exhausted retries
        var newItems = items
            .Where(i => !_processed.ContainsKey(i.Id))
            .Where(i => !i.Tags.Any(t => t.Equals(_config.ProcessedTag, StringComparison.OrdinalIgnoreCase)))
            .Where(i => !i.Tags.Any(t => t.Equals(_config.ReviewTag, StringComparison.OrdinalIgnoreCase)))
            .Where(i => !_retryPolicy.IsBackoffActive(i.Id))
            .Where(i => !_retryPolicy.IsExhausted(i.Id))
            .ToList();

        if (newItems.Count == 0)
        {
            _logger.LogDebug("No new work items found");
            return;
        }

        // Classify first for priority scoring, then sort by priority
        foreach (var item in newItems) _router.Classify(item);
        newItems = WorkItemPriority.Sort(newItems);

        AutopilotMetrics.SetPollItemsFound(newItems.Count);
        _logger.LogInformation("Found {Count} new work items to process", newItems.Count);

        var tasks = newItems.Select(item => ProcessWorkItemAsync(item, ct));
        await Task.WhenAll(tasks);
    }

    private async Task ProcessWorkItemAsync(WorkItemInfo item, CancellationToken ct)
    {
        await _concurrencyGate.WaitAsync(ct);
        try
        {
            // Mark as processing
            _processed[item.Id] = DateTime.UtcNow;

            // Classify
            var classified = _router.Classify(item);
            _logger.LogInformation("Processing: {Item} → Category: {Category}", item, classified.Category);

            // Route to skill
            var skill = _router.Route(classified);
            if (skill == null)
            {
                _logger.LogWarning("No skill found for {Item}", item);
                return;
            }

            // RBAC checks
            if (!_rbac.IsUserAllowed(item))
            {
                _logger.LogWarning("RBAC denied: #{Id} created by '{User}'", item.Id, item.CreatedBy);
                return;
            }
            if (!_rbac.IsSkillAllowed(skill))
            {
                _logger.LogWarning("RBAC denied skill '{Skill}' for #{Id}", skill, item.Id);
                return;
            }

            _logger.LogInformation("Routing #{Id} to skill: {Skill}", item.Id, skill);

            // Decompose requirements instead of executing directly
            if (classified.Category == TaskCategory.Requirement && _config.AutoDecompose)
            {
                await _decomposer.DecomposeAsync(classified, ct);
                await _ado.AddTagAsync(item.Id, _config.ProcessedTag, ct);
                _logger.LogInformation("#{Id} decomposed into child tasks", item.Id);
                return;
            }

            // Notify: starting
            await _notifier.NotifyStartedAsync(item, skill, ct);

            // Save execution record (start)
            var execRecord = await _execRepo.StartExecutionAsync(item, skill);

            // Execute
            ExecutionResult result;
            if (_config.DryRun)
            {
                _logger.LogInformation("[DRY-RUN] Would execute: claude {Skill} for #{Id}", skill, item.Id);
                result = ExecutionResult.Ok(item.Id, skill, "[DRY-RUN] Skipped execution");
                result.Duration = TimeSpan.FromMilliseconds(100);
            }
            else
            {
                result = await _executor.ExecuteAsync(item, skill, draftPr: _config.RequireApproval, ct);
            }

            // Save execution record (complete)
            await _execRepo.CompleteExecutionAsync(execRecord.Id, result);

            if (result.Success)
            {
                _retryPolicy.RecordSuccess(item.Id);

                if (_config.RequireApproval && !string.IsNullOrEmpty(result.PrUrl))
                {
                    // Draft PR created — mark for review, don't finalize
                    await _ado.AddTagAsync(item.Id, _config.ReviewTag, ct);
                    await _ado.AddCommentAsync(item.Id,
                        $"<b>🔍 PR created (draft)</b>, awaiting human review.<br/>PR: <a href=\"{result.PrUrl}\">{result.PrUrl}</a>", ct);
                    await _notifier.NotifyCompletedAsync(item, result, markProcessed: false, ct);
                }
                else
                {
                    await _notifier.NotifyCompletedAsync(item, result, markProcessed: true, ct);
                }
            }
            else
            {
                _retryPolicy.RecordFailure(item.Id, result.Error ?? "Unknown error");
                var exhausted = _retryPolicy.IsExhausted(item.Id);
                await _notifier.NotifyCompletedAsync(item, result, markProcessed: exhausted, ct);

                if (exhausted)
                {
                    var state = _retryPolicy.GetState(item.Id);
                    _logger.LogError("#{Id} failed after {Count} retries — marking as done", item.Id, state?.RetryCount);
                    await _ado.AddCommentAsync(item.Id,
                        $"<b>⛔ Autopilot gave up after {state?.RetryCount} retries.</b> Last error: {result.Error}", ct);
                }
                else
                {
                    // Don't mark as processed — will be retried next cycle (after backoff)
                    _processed.TryRemove(item.Id, out _);
                }
            }

            var status = result.Success ? "SUCCESS" : "FAILED";
            AutopilotMetrics.RecordTask(status.ToLower(), classified.Category.ToString(), skill);
            AutopilotMetrics.RecordDuration(classified.Category.ToString(), result.Duration.TotalSeconds);
            _logger.LogInformation("#{Id} {Status} in {Duration:mm\\:ss} — Skill: {Skill}",
                item.Id, status, result.Duration, skill);
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            _logger.LogError(ex, "Error processing #{Id}", item.Id);
            await _notifier.NotifyErrorAsync(item, ex.Message, ct);
        }
        finally
        {
            _concurrencyGate.Release();
        }
    }
}
