namespace AdoAutopilot.Data.Entities;

public class ExecutionRecord
{
    public int Id { get; set; }
    public int WorkItemId { get; set; }
    public string Title { get; set; } = string.Empty;
    public string Category { get; set; } = string.Empty;
    public string SkillUsed { get; set; } = string.Empty;
    public ExecutionStatus Status { get; set; }
    public string? BranchName { get; set; }
    public string? PrUrl { get; set; }
    public string? FilesChanged { get; set; } // JSON array
    public string? Error { get; set; }
    public string? Output { get; set; }
    public double DurationSeconds { get; set; }
    public DateTime StartedAt { get; set; }
    public DateTime? CompletedAt { get; set; }
    public int RetryCount { get; set; }
    public long CostTokens { get; set; }
}

public enum ExecutionStatus
{
    Pending,
    Running,
    Success,
    Failed,
    Retrying
}
