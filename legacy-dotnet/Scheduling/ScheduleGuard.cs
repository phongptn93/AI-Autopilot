using AdoAutopilot.Models;
using Microsoft.Extensions.Options;

namespace AdoAutopilot.Scheduling;

public class ScheduleGuard
{
    private readonly AutopilotConfig _config;
    private readonly ILogger<ScheduleGuard> _logger;

    public ScheduleGuard(IOptions<AutopilotConfig> config, ILogger<ScheduleGuard> logger)
    {
        _config = config.Value;
        _logger = logger;
    }

    public bool IsWithinWindow(DateTime? utcNow = null)
    {
        if (string.IsNullOrEmpty(_config.ScheduleStart) || string.IsNullOrEmpty(_config.ScheduleEnd))
            return true; // No schedule configured — always allowed

        var now = utcNow ?? DateTime.UtcNow;

        // Check day of week
        var allowedDays = _config.ScheduleDays
            .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Select(d => Enum.TryParse<DayOfWeek>(d, true, out var day) ? day : (DayOfWeek?)null)
            .Where(d => d.HasValue)
            .Select(d => d!.Value)
            .ToHashSet();

        if (allowedDays.Count > 0 && !allowedDays.Contains(now.DayOfWeek))
        {
            _logger.LogDebug("Outside schedule: {Day} not in allowed days", now.DayOfWeek);
            return false;
        }

        // Check time window
        if (TimeOnly.TryParse(_config.ScheduleStart, out var start) &&
            TimeOnly.TryParse(_config.ScheduleEnd, out var end))
        {
            var currentTime = TimeOnly.FromDateTime(now);
            var inWindow = start <= end
                ? currentTime >= start && currentTime <= end
                : currentTime >= start || currentTime <= end; // overnight window

            if (!inWindow)
                _logger.LogDebug("Outside schedule: {Time} not in {Start}-{End}", currentTime, start, end);

            return inWindow;
        }

        return true; // Can't parse → allow
    }
}
