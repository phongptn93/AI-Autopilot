using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using AdoAutopilot.Models;
using Microsoft.Extensions.Options;

namespace AdoAutopilot.Notifications;

/// <summary>
/// Zalo OA notification via Zalo OA API.
/// Setup: https://oa.zalo.me → Create OA → Get Access Token → Get recipient user ID.
/// </summary>
public class ZaloNotifier : INotificationChannel
{
    private readonly AutopilotConfig _config;
    private readonly ILogger<ZaloNotifier> _logger;
    private readonly HttpClient _http;

    private const string ZaloApiUrl = "https://openapi.zalo.me/v3.0/oa/message/cs";

    public string Name => "Zalo OA";
    public bool IsEnabled => !string.IsNullOrEmpty(_config.ZaloOaAccessToken)
                          && !string.IsNullOrEmpty(_config.ZaloRecipientUserId);

    public ZaloNotifier(IOptions<AutopilotConfig> config, ILogger<ZaloNotifier> logger, IHttpClientFactory httpFactory)
    {
        _config = config.Value;
        _logger = logger;
        _http = httpFactory.CreateClient("ZaloNotifier");
    }

    public async Task SendAsync(NotificationMessage message, CancellationToken ct = default)
    {
        if (!IsEnabled) return;

        var text = $"{message.Title}\n\n{message.Summary}";

        var payload = new
        {
            recipient = new { user_id = _config.ZaloRecipientUserId },
            message = new
            {
                text
            }
        };

        var json = JsonSerializer.Serialize(payload);
        var content = new StringContent(json, Encoding.UTF8, "application/json");

        var request = new HttpRequestMessage(HttpMethod.Post, ZaloApiUrl)
        {
            Content = content
        };
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _config.ZaloOaAccessToken);

        try
        {
            var response = await _http.SendAsync(request, ct);
            var body = await response.Content.ReadAsStringAsync(ct);

            if (!response.IsSuccessStatusCode)
            {
                _logger.LogWarning("Zalo OA send failed: {Status} {Body}", response.StatusCode, body);
            }
            else
            {
                // Zalo returns {"error":0,"message":"Success"} on success
                var result = JsonSerializer.Deserialize<JsonElement>(body);
                var errorCode = result.TryGetProperty("error", out var err) ? err.GetInt32() : -1;
                if (errorCode != 0)
                    _logger.LogWarning("Zalo OA returned error: {Body}", body);
                else
                    _logger.LogDebug("Zalo notification sent: {Title}", message.Title);
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Zalo notification failed");
        }
    }
}
