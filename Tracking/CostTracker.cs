using System.Text.RegularExpressions;
using AdoAutopilot.Data;
using AdoAutopilot.Data.Entities;
using AdoAutopilot.Models;
using AdoAutopilot.Notifications;
using Microsoft.Extensions.Options;

namespace AdoAutopilot.Tracking;

public class CostTracker
{
    private readonly ExecutionRepository _repo;
    private readonly AutopilotConfig _config;
    private readonly IEnumerable<INotificationChannel> _channels;
    private readonly ILogger<CostTracker> _logger;
    private long _dailyTokens;
    private DateTime _dailyResetDate = DateTime.UtcNow.Date;
    private bool _alertSent;

    public CostTracker(
        ExecutionRepository repo,
        IOptions<AutopilotConfig> config,
        IEnumerable<INotificationChannel> channels,
        ILogger<CostTracker> logger)
    {
        _repo = repo;
        _config = config.Value;
        _channels = channels;
        _logger = logger;
    }

    public static long ParseTokenCount(string claudeOutput)
    {
        // Try to find token usage patterns in Claude CLI output
        // Common patterns: "tokens used: 1234", "1234 tokens", "input_tokens: 500, output_tokens: 200"
        long total = 0;
        var matches = Regex.Matches(claudeOutput, @"(\d[\d,]+)\s*tokens?", RegexOptions.IgnoreCase);
        foreach (Match m in matches)
        {
            if (long.TryParse(m.Groups[1].Value.Replace(",", ""), out var count))
                total += count;
        }
        return total;
    }

    public async Task TrackAsync(int recordId, string claudeOutput)
    {
        var tokens = ParseTokenCount(claudeOutput);
        if (tokens <= 0) return;

        await _repo.UpdateCostAsync(recordId, tokens);

        // Reset daily counter if new day
        var today = DateTime.UtcNow.Date;
        if (today != _dailyResetDate)
        {
            _dailyTokens = 0;
            _dailyResetDate = today;
            _alertSent = false;
        }

        _dailyTokens += tokens;
        _logger.LogDebug("Cost: {Tokens} tokens (daily total: {Daily})", tokens, _dailyTokens);

        // Budget alert
        if (_config.CostAlertEnabled && _config.DailyBudgetTokens > 0 && _dailyTokens > _config.DailyBudgetTokens && !_alertSent)
        {
            _alertSent = true;
            _logger.LogWarning("⚠ Daily token budget exceeded: {Used}/{Budget}",
                _dailyTokens, _config.DailyBudgetTokens);

            foreach (var channel in _channels.Where(c => c.IsEnabled))
            {
                try
                {
                    await channel.SendAsync(new NotificationMessage
                    {
                        WorkItem = new WorkItemInfo { Id = 0, Title = "Budget Alert" },
                        Type = NotificationType.Error,
                        Error = $"Daily token budget exceeded: {_dailyTokens:N0} / {_config.DailyBudgetTokens:N0} tokens"
                    });
                }
                catch { /* best effort */ }
            }
        }
    }
}
