using AdoAutopilot.Models;
using AdoAutopilot.Notifications;
using Microsoft.Extensions.Options;

namespace AdoAutopilot.Ado;

/// <summary>
/// Updates ADO work items and sends notifications (Teams, Zalo) with autopilot execution results.
/// </summary>
public class AdoNotifier
{
    private readonly AdoClient _ado;
    private readonly AutopilotConfig _config;
    private readonly IEnumerable<INotificationChannel> _channels;
    private readonly ILogger<AdoNotifier> _logger;

    public AdoNotifier(
        AdoClient ado,
        IOptions<AutopilotConfig> config,
        IEnumerable<INotificationChannel> channels,
        ILogger<AdoNotifier> logger)
    {
        _ado = ado;
        _config = config.Value;
        _channels = channels;
        _logger = logger;
    }

    public async Task NotifyStartedAsync(WorkItemInfo item, string skill, CancellationToken ct = default)
    {
        var comment = $"""
            <div>
            <b>🤖 ADO Autopilot — Processing</b><br/>
            <ul>
            <li><b>Skill:</b> <code>{skill}</code></li>
            <li><b>Category:</b> {item.Category}</li>
            <li><b>Started:</b> {DateTime.UtcNow:yyyy-MM-dd HH:mm:ss} UTC</li>
            </ul>
            </div>
            """;

        if (_config.DryRun)
        {
            _logger.LogInformation("[DRY-RUN] Would comment on #{Id}: Started", item.Id);
            return;
        }

        await _ado.AddCommentAsync(item.Id, comment, ct);
        await _ado.UpdateStateAsync(item.Id, "Active", ct);

        await BroadcastAsync(new NotificationMessage
        {
            WorkItem = item,
            Type = NotificationType.Started,
            Skill = skill
        }, ct);
    }

    public Task NotifyCompletedAsync(WorkItemInfo item, ExecutionResult result, CancellationToken ct = default)
        => NotifyCompletedAsync(item, result, markProcessed: true, ct);

    public async Task NotifyCompletedAsync(WorkItemInfo item, ExecutionResult result, bool markProcessed, CancellationToken ct = default)
    {
        string comment;

        if (result.Success)
        {
            var filesHtml = result.FilesChanged.Count > 0
                ? "<li><b>Files changed:</b><ul>" +
                  string.Join("", result.FilesChanged.Take(20).Select(f => $"<li><code>{f}</code></li>")) +
                  (result.FilesChanged.Count > 20 ? $"<li>...and {result.FilesChanged.Count - 20} more</li>" : "") +
                  "</ul></li>"
                : "";

            var prHtml = !string.IsNullOrEmpty(result.PrUrl)
                ? $"<li><b>PR:</b> <a href=\"{result.PrUrl}\">{result.PrUrl}</a></li>"
                : "";

            comment = $"""
                <div>
                <b>✅ ADO Autopilot — Completed</b><br/>
                <ul>
                <li><b>Skill:</b> <code>{result.SkillUsed}</code></li>
                <li><b>Duration:</b> {result.Duration:mm\:ss}</li>
                <li><b>Branch:</b> <code>{result.BranchName}</code></li>
                {filesHtml}
                {prHtml}
                </ul>
                </div>
                """;
        }
        else
        {
            comment = $"""
                <div>
                <b>❌ ADO Autopilot — Failed</b><br/>
                <ul>
                <li><b>Skill:</b> <code>{result.SkillUsed}</code></li>
                <li><b>Duration:</b> {result.Duration:mm\:ss}</li>
                <li><b>Error:</b> {result.Error}</li>
                </ul>
                </div>
                """;
        }

        if (_config.DryRun)
        {
            var status = result.Success ? "Completed" : "Failed";
            _logger.LogInformation("[DRY-RUN] Would comment on #{Id}: {Status}", item.Id, status);
            return;
        }

        await _ado.AddCommentAsync(item.Id, comment, ct);

        // Mark as processed only if requested (retry may skip this)
        if (markProcessed)
        {
            await _ado.AddTagAsync(item.Id, _config.ProcessedTag, ct);

            // Transition state if succeeded with PR
            if (result.Success && !string.IsNullOrEmpty(result.PrUrl))
            {
                await _ado.UpdateStateAsync(item.Id, "Resolved", ct);
            }
        }

        await BroadcastAsync(new NotificationMessage
        {
            WorkItem = item,
            Type = NotificationType.Completed,
            Result = result
        }, ct);
    }

    public async Task NotifyErrorAsync(WorkItemInfo item, string error, CancellationToken ct = default)
    {
        var comment = $"""
            <div>
            <b>⚠️ ADO Autopilot — Error</b><br/>
            <p>{error}</p>
            </div>
            """;

        if (_config.DryRun)
        {
            _logger.LogInformation("[DRY-RUN] Would comment on #{Id}: Error — {Error}", item.Id, error);
            return;
        }

        await _ado.AddCommentAsync(item.Id, comment, ct);

        await BroadcastAsync(new NotificationMessage
        {
            WorkItem = item,
            Type = NotificationType.Error,
            Error = error
        }, ct);
    }

    private async Task BroadcastAsync(NotificationMessage message, CancellationToken ct)
    {
        foreach (var channel in _channels.Where(c => c.IsEnabled))
        {
            try
            {
                await channel.SendAsync(message, ct);
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "Notification via {Channel} failed", channel.Name);
            }
        }
    }
}
