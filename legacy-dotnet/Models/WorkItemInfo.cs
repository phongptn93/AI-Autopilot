namespace AdoAutopilot.Models;

public class WorkItemInfo
{
    public int Id { get; set; }
    public string Title { get; set; } = string.Empty;
    public string WorkItemType { get; set; } = string.Empty;
    public string State { get; set; } = string.Empty;
    public string? AssignedTo { get; set; }
    public string? Description { get; set; }
    public string? AcceptanceCriteria { get; set; }
    public int? ParentId { get; set; }
    public List<string> Tags { get; set; } = new();
    public string? AreaPath { get; set; }
    public string? IterationPath { get; set; }
    public DateTime ChangedDate { get; set; }
    public string? CreatedBy { get; set; }

    /// <summary>ADO Priority field (1=Critical, 2=High, 3=Normal, 4=Low)</summary>
    public int Priority { get; set; }

    /// <summary>Detected task category from title/tags</summary>
    public TaskCategory Category { get; set; }

    public override string ToString() => $"#{Id} [{WorkItemType}] {Title}";
}

public enum TaskCategory
{
    Unknown,
    BackendTask,      // [BE] in title or tag
    FrontendTask,     // [FE] in title or tag
    Bug,              // Work item type = Bug
    DatabaseTask,     // [DB] in title or tag
    TestTask,         // [QC] / [Test] in title or tag
    Requirement       // Work item type = User Story / Requirement
}
