namespace AdoAutopilot.Models;

public class ExecutionResult
{
    public int WorkItemId { get; set; }
    public bool Success { get; set; }
    public string SkillUsed { get; set; } = string.Empty;
    public string Output { get; set; } = string.Empty;
    public string? Error { get; set; }
    public string? BranchName { get; set; }
    public string? PrUrl { get; set; }
    public List<string> FilesChanged { get; set; } = new();
    public TimeSpan Duration { get; set; }
    public DateTime CompletedAt { get; set; } = DateTime.UtcNow;

    public static ExecutionResult Ok(int workItemId, string skill, string output) => new()
    {
        WorkItemId = workItemId,
        Success = true,
        SkillUsed = skill,
        Output = output
    };

    public static ExecutionResult Fail(int workItemId, string skill, string error) => new()
    {
        WorkItemId = workItemId,
        Success = false,
        SkillUsed = skill,
        Error = error
    };
}
