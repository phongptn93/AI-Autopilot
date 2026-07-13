using System.Text.RegularExpressions;
using AdoAutopilot.Models;

namespace AdoAutopilot.Routing;

/// <summary>
/// Classifies work items and routes them to the appropriate Claude Code skill.
/// </summary>
public class TaskRouter
{
    private readonly ILogger<TaskRouter> _logger;

    // Skill command templates — these invoke skills already in .claude/skills/
    private static readonly Dictionary<TaskCategory, string> SkillMap = new()
    {
        [TaskCategory.BackendTask]  = "/implement-task-be {id}",
        [TaskCategory.FrontendTask] = "/implement-task-fe {id}",
        [TaskCategory.Bug]          = "/bugfix-workflow {id}",
        [TaskCategory.TestTask]     = "/qc-test-management {id}",
        [TaskCategory.Requirement]  = "/analyze-requirement {id}",
    };

    public TaskRouter(ILogger<TaskRouter> logger)
    {
        _logger = logger;
    }

    /// <summary>
    /// Classify a work item into a TaskCategory based on title, type, and tags.
    /// </summary>
    public WorkItemInfo Classify(WorkItemInfo item)
    {
        item.Category = DetectCategory(item);
        return item;
    }

    /// <summary>
    /// Route a classified work item to a Claude skill command string.
    /// Returns null if no skill matches.
    /// </summary>
    public string? Route(WorkItemInfo item)
    {
        if (item.Category == TaskCategory.Unknown || item.Category == TaskCategory.DatabaseTask)
        {
            _logger.LogWarning("Cannot route #{Id}: category={Category}", item.Id, item.Category);
            return null;
        }

        if (!SkillMap.TryGetValue(item.Category, out var template))
            return null;

        return template.Replace("{id}", item.Id.ToString());
    }

    private static TaskCategory DetectCategory(WorkItemInfo item)
    {
        var title = item.Title.ToUpperInvariant();
        var tags = string.Join(" ", item.Tags).ToUpperInvariant();
        var type = item.WorkItemType.ToUpperInvariant();

        // Bug type takes priority
        if (type == "BUG") return TaskCategory.Bug;

        // Check by title prefix — NOIS convention: [BE], [FE], [DB], [QC]
        if (HasPrefix(title, "[BE]") || HasTag(tags, "BE") || HasTag(tags, "BACKEND"))
            return TaskCategory.BackendTask;

        if (HasPrefix(title, "[FE]") || HasTag(tags, "FE") || HasTag(tags, "FRONTEND"))
            return TaskCategory.FrontendTask;

        if (HasPrefix(title, "[DB]") || HasTag(tags, "DB") || HasTag(tags, "DATABASE"))
            return TaskCategory.DatabaseTask;

        if (HasPrefix(title, "[QC]") || HasPrefix(title, "[TEST]") || HasTag(tags, "QC") || HasTag(tags, "TEST"))
            return TaskCategory.TestTask;

        // Check work item type
        if (type is "USER STORY" or "REQUIREMENT" or "FEATURE")
            return TaskCategory.Requirement;

        if (type == "TASK")
        {
            // Try to infer from title keywords
            if (ContainsAny(title, "API", "CONTROLLER", "SERVICE", "ENTITY", "MIGRATION", "ENDPOINT"))
                return TaskCategory.BackendTask;
            if (ContainsAny(title, "COMPONENT", "PAGE", "FORM", "UI", "ANGULAR", "SCREEN"))
                return TaskCategory.FrontendTask;
        }

        return TaskCategory.Unknown;
    }

    private static bool HasPrefix(string title, string prefix)
        => title.StartsWith(prefix, StringComparison.OrdinalIgnoreCase);

    private static bool HasTag(string tags, string tag)
        => Regex.IsMatch(tags, $@"\b{Regex.Escape(tag)}\b");

    private static bool ContainsAny(string text, params string[] keywords)
        => keywords.Any(k => text.Contains(k, StringComparison.OrdinalIgnoreCase));
}
