using Prometheus;

namespace AdoAutopilot.Metrics;

public static class AutopilotMetrics
{
    private static readonly Counter TasksTotal = Prometheus.Metrics.CreateCounter(
        "autopilot_tasks_total",
        "Total tasks processed",
        new CounterConfiguration { LabelNames = new[] { "status", "category", "skill" } });

    private static readonly Histogram TaskDuration = Prometheus.Metrics.CreateHistogram(
        "autopilot_task_duration_seconds",
        "Task execution duration in seconds",
        new HistogramConfiguration
        {
            LabelNames = new[] { "category" },
            Buckets = Histogram.ExponentialBuckets(10, 2, 10) // 10s, 20s, 40s, ... ~5120s
        });

    private static readonly Gauge PollItemsFound = Prometheus.Metrics.CreateGauge(
        "autopilot_poll_items_found",
        "Number of items found in last poll");

    private static readonly Gauge ActiveExecutions = Prometheus.Metrics.CreateGauge(
        "autopilot_active_executions",
        "Number of currently running executions");

    private static readonly Counter RetryTotal = Prometheus.Metrics.CreateCounter(
        "autopilot_retry_total",
        "Total retry attempts");

    private static readonly Counter CostTokensTotal = Prometheus.Metrics.CreateCounter(
        "autopilot_cost_tokens_total",
        "Total tokens consumed");

    public static void RecordTask(string status, string category, string skill)
        => TasksTotal.WithLabels(status, category, skill).Inc();

    public static void RecordDuration(string category, double seconds)
        => TaskDuration.WithLabels(category).Observe(seconds);

    public static void SetPollItemsFound(int count)
        => PollItemsFound.Set(count);

    public static void IncrementActiveExecutions()
        => ActiveExecutions.Inc();

    public static void DecrementActiveExecutions()
        => ActiveExecutions.Dec();

    public static void RecordRetry()
        => RetryTotal.Inc();

    public static void RecordCost(long tokens)
        => CostTokensTotal.Inc(tokens);
}
