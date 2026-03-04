using System.Collections.Concurrent;

namespace AdoAutopilot.Execution;

public class RetryState
{
    public int RetryCount { get; set; }
    public DateTime LastAttempt { get; set; }
    public string? LastError { get; set; }
}

public class RetryPolicy
{
    private readonly ConcurrentDictionary<int, RetryState> _states = new();
    private readonly int _maxRetries;
    private readonly int _backoffSeconds;
    private readonly ILogger<RetryPolicy> _logger;

    public RetryPolicy(int maxRetries, int backoffSeconds, ILogger<RetryPolicy> logger)
    {
        _maxRetries = maxRetries;
        _backoffSeconds = backoffSeconds;
        _logger = logger;
    }

    public bool ShouldRetry(int workItemId) =>
        !_states.TryGetValue(workItemId, out var s) || s.RetryCount < _maxRetries;

    public bool IsBackoffActive(int workItemId)
    {
        if (!_states.TryGetValue(workItemId, out var s)) return false;
        var delay = TimeSpan.FromSeconds(_backoffSeconds * Math.Pow(2, s.RetryCount - 1));
        return DateTime.UtcNow - s.LastAttempt < delay;
    }

    public void RecordFailure(int workItemId, string error)
    {
        _states.AddOrUpdate(workItemId,
            _ => new RetryState { RetryCount = 1, LastAttempt = DateTime.UtcNow, LastError = error },
            (_, existing) =>
            {
                existing.RetryCount++;
                existing.LastAttempt = DateTime.UtcNow;
                existing.LastError = error;
                return existing;
            });

        var state = _states[workItemId];
        _logger.LogWarning("#{Id} failed (attempt {Count}/{Max}): {Error}",
            workItemId, state.RetryCount, _maxRetries, error);
    }

    public void RecordSuccess(int workItemId) => _states.TryRemove(workItemId, out _);

    public bool IsExhausted(int workItemId) =>
        _states.TryGetValue(workItemId, out var s) && s.RetryCount >= _maxRetries;

    public RetryState? GetState(int workItemId) =>
        _states.TryGetValue(workItemId, out var s) ? s : null;
}
