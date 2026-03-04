using AdoAutopilot.Models;
using Microsoft.Extensions.Options;

namespace AdoAutopilot.Execution;

public class FeedbackHandler
{
    private readonly ClaudeExecutor _executor;
    private readonly AutopilotConfig _config;
    private readonly ILogger<FeedbackHandler> _logger;

    public FeedbackHandler(ClaudeExecutor executor, IOptions<AutopilotConfig> config, ILogger<FeedbackHandler> logger)
    {
        _executor = executor;
        _config = config.Value;
        _logger = logger;
    }

    public async Task<ExecutionResult> HandleFeedbackAsync(WorkItemInfo item, string branchName, string feedback, int revision, CancellationToken ct)
    {
        _logger.LogInformation("#{Id} Handling feedback (revision {Rev}): {Feedback}",
            item.Id, revision, feedback[..Math.Min(feedback.Length, 200)]);

        var prompt = $"/bugfix-workflow {item.Id} — PR feedback to address: {feedback}";
        var result = await _executor.ExecuteAsync(item, prompt, draftPr: _config.RequireApproval, ct);

        if (result.Success)
            _logger.LogInformation("#{Id} Feedback addressed (revision {Rev})", item.Id, revision);
        else
            _logger.LogWarning("#{Id} Failed to address feedback (revision {Rev}): {Error}", item.Id, revision, result.Error);

        return result;
    }
}
