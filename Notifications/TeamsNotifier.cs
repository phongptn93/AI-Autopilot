using System.Text;
using System.Text.Json;
using AdoAutopilot.Models;
using Microsoft.Extensions.Options;

namespace AdoAutopilot.Notifications;

/// <summary>
/// MS Teams notification via Workflows Webhook (Adaptive Card format).
/// Setup: Teams channel → ⋯ → Workflows → "Post to a channel when a webhook request is received" → copy URL.
/// </summary>
public class TeamsNotifier : INotificationChannel
{
    private readonly AutopilotConfig _config;
    private readonly ILogger<TeamsNotifier> _logger;
    private readonly HttpClient _http;

    public string Name => "MS Teams";
    public bool IsEnabled => !string.IsNullOrEmpty(_config.TeamsWebhookUrl);

    public TeamsNotifier(IOptions<AutopilotConfig> config, ILogger<TeamsNotifier> logger, IHttpClientFactory httpFactory)
    {
        _config = config.Value;
        _logger = logger;
        _http = httpFactory.CreateClient("TeamsNotifier");
    }

    public async Task SendAsync(NotificationMessage message, CancellationToken ct = default)
    {
        if (!IsEnabled) return;

        var payload = BuildPayload(message);
        var json = JsonSerializer.Serialize(payload);
        var content = new StringContent(json, Encoding.UTF8, "application/json");

        try
        {
            var response = await _http.PostAsync(_config.TeamsWebhookUrl, content, ct);
            if (!response.IsSuccessStatusCode)
            {
                var body = await response.Content.ReadAsStringAsync(ct);
                _logger.LogWarning("Teams webhook failed: {Status} {Body}", response.StatusCode, body);
            }
            else
            {
                _logger.LogDebug("Teams notification sent: {Title}", message.Title);
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Teams notification failed");
        }
    }

    private static object BuildPayload(NotificationMessage message)
    {
        var color = message.Type switch
        {
            NotificationType.Started => "accent",
            NotificationType.Completed when message.Result?.Success == true => "good",
            NotificationType.Completed => "attention",
            NotificationType.Error => "warning",
            _ => "default"
        };

        var facts = new List<object>
        {
            new { title = "Work Item", value = $"#{message.WorkItem.Id} {message.WorkItem.Title}" },
            new { title = "Type", value = message.WorkItem.WorkItemType },
            new { title = "Category", value = message.WorkItem.Category.ToString() }
        };

        if (!string.IsNullOrEmpty(message.Skill))
            facts.Add(new { title = "Skill", value = message.Skill });

        if (message.Result != null)
        {
            facts.Add(new { title = "Duration", value = message.Result.Duration.ToString(@"mm\:ss") });
            if (!string.IsNullOrEmpty(message.Result.BranchName))
                facts.Add(new { title = "Branch", value = message.Result.BranchName });
            if (!string.IsNullOrEmpty(message.Result.PrUrl))
                facts.Add(new { title = "PR", value = message.Result.PrUrl });
            if (!string.IsNullOrEmpty(message.Result.Error))
                facts.Add(new { title = "Error", value = message.Result.Error });
        }

        if (!string.IsNullOrEmpty(message.Error))
            facts.Add(new { title = "Error", value = message.Error });

        // Workflows webhook expects: { "type": "message", "attachments": [{ Adaptive Card }] }
        return new
        {
            type = "message",
            attachments = new[]
            {
                new
                {
                    contentType = "application/vnd.microsoft.card.adaptive",
                    contentUrl = (string?)null,
                    content = new
                    {
                        type = "AdaptiveCard",
                        body = new object[]
                        {
                            new
                            {
                                type = "TextBlock",
                                size = "Medium",
                                weight = "Bolder",
                                text = message.Title,
                                color
                            },
                            new
                            {
                                type = "FactSet",
                                facts
                            }
                        },
                        schema = "http://adaptivecards.io/schemas/adaptive-card.json",
                        version = "1.4"
                    }
                }
            }
        };
    }
}
