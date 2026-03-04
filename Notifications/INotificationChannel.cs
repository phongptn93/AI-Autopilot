using AdoAutopilot.Models;

namespace AdoAutopilot.Notifications;

public interface INotificationChannel
{
    string Name { get; }
    bool IsEnabled { get; }
    Task SendAsync(NotificationMessage message, CancellationToken ct = default);
}

public class NotificationMessage
{
    public WorkItemInfo WorkItem { get; set; } = null!;
    public NotificationType Type { get; set; }
    public string Skill { get; set; } = string.Empty;
    public ExecutionResult? Result { get; set; }
    public string? Error { get; set; }

    public string Title => Type switch
    {
        NotificationType.Started => $"🤖 Processing #{WorkItem.Id}",
        NotificationType.Completed => Result?.Success == true
            ? $"✅ Completed #{WorkItem.Id}"
            : $"❌ Failed #{WorkItem.Id}",
        NotificationType.Error => $"⚠️ Error #{WorkItem.Id}",
        _ => $"📋 #{WorkItem.Id}"
    };

    public string Summary => Type switch
    {
        NotificationType.Started =>
            $"**{WorkItem.Title}**\nSkill: `{Skill}` | Category: {WorkItem.Category}",
        NotificationType.Completed when Result?.Success == true =>
            $"**{WorkItem.Title}**\nSkill: `{Result.SkillUsed}` | Duration: {Result.Duration:mm\\:ss}" +
            (!string.IsNullOrEmpty(Result.PrUrl) ? $"\nPR: {Result.PrUrl}" : "") +
            (Result.FilesChanged.Count > 0 ? $"\nFiles: {Result.FilesChanged.Count} changed" : ""),
        NotificationType.Completed =>
            $"**{WorkItem.Title}**\nSkill: `{Result?.SkillUsed}` | Error: {Result?.Error}",
        NotificationType.Error =>
            $"**{WorkItem.Title}**\n{Error}",
        _ => WorkItem.Title
    };
}

public enum NotificationType
{
    Started,
    Completed,
    Error
}
