using System.Text.Json;
using AdoAutopilot.Execution;
using AdoAutopilot.Models;
using Microsoft.Extensions.Options;

namespace AdoAutopilot.Ado;

public class PrMonitorService : BackgroundService
{
    private readonly AdoClient _ado;
    private readonly AdoNotifier _notifier;
    private readonly FeedbackHandler _feedback;
    private readonly AutopilotConfig _config;
    private readonly ILogger<PrMonitorService> _logger;

    // Track revision counts per work item
    private readonly Dictionary<int, int> _revisionCounts = new();

    public PrMonitorService(
        AdoClient ado,
        AdoNotifier notifier,
        FeedbackHandler feedback,
        IOptions<AutopilotConfig> config,
        ILogger<PrMonitorService> logger)
    {
        _ado = ado;
        _notifier = notifier;
        _feedback = feedback;
        _config = config.Value;
        _logger = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        if (!_config.FeedbackLoopEnabled)
        {
            _logger.LogInformation("PR Feedback Loop disabled");
            return;
        }

        _logger.LogInformation("PR Monitor started — polling for rejected PRs");

        while (!ct.IsCancellationRequested)
        {
            try
            {
                // Poll interval: 2 minutes (less frequent than main poller)
                await Task.Delay(TimeSpan.FromMinutes(2), ct);

                // Find work items tagged with review tag (they have PRs)
                var items = await _ado.GetPendingWorkItemsAsync(ct);
                var reviewItems = items
                    .Where(i => i.Tags.Any(t => t.Equals(_config.ReviewTag, StringComparison.OrdinalIgnoreCase)))
                    .ToList();

                // For now, this monitors ADO comments for feedback keywords
                // Full PR API integration would check PR threads for "changes requested" vote
                foreach (var item in reviewItems)
                {
                    var revisionCount = _revisionCounts.GetValueOrDefault(item.Id, 0);
                    if (revisionCount >= _config.MaxRevisions) continue;

                    // In a full implementation, this would:
                    // 1. Check PR status via ADO Git API
                    // 2. Fetch review comments with "changes requested"
                    // 3. Pass feedback to FeedbackHandler
                    // For now, this is a placeholder for the feedback loop infrastructure
                }
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                _logger.LogError(ex, "PR Monitor cycle failed");
            }
        }
    }
}
