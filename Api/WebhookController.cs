using System.Collections.Concurrent;
using System.Text.Json;
using AdoAutopilot.Ado;
using AdoAutopilot.Models;

namespace AdoAutopilot.Api;

public static class WebhookController
{
    // External queue: webhook pushes work item IDs here, poller picks them up
    private static readonly ConcurrentQueue<int> _webhookQueue = new();

    public static bool TryDequeue(out int workItemId) => _webhookQueue.TryDequeue(out workItemId);
    public static int QueueCount => _webhookQueue.Count;

    public static void MapWebhookEndpoints(this WebApplication app)
    {
        app.MapPost("/api/webhook/ado", async (HttpContext ctx, AdoClient ado, ILogger<AdoClient> logger) =>
        {
            using var reader = new StreamReader(ctx.Request.Body);
            var body = await reader.ReadToEndAsync();

            try
            {
                var payload = JsonDocument.Parse(body);
                var root = payload.RootElement;

                // ADO Service Hook payload: resource.workItemId or resource.id
                int? workItemId = null;
                if (root.TryGetProperty("resource", out var resource))
                {
                    if (resource.TryGetProperty("workItemId", out var wiId))
                        workItemId = wiId.GetInt32();
                    else if (resource.TryGetProperty("id", out var id))
                        workItemId = id.GetInt32();
                }

                if (workItemId == null)
                {
                    logger.LogWarning("Webhook received but no workItemId found in payload");
                    return Results.BadRequest(new { error = "No workItemId in payload" });
                }

                _webhookQueue.Enqueue(workItemId.Value);
                logger.LogInformation("Webhook queued work item #{Id}", workItemId.Value);
                return Results.Ok(new { queued = workItemId.Value });
            }
            catch (JsonException ex)
            {
                logger.LogWarning(ex, "Invalid webhook payload");
                return Results.BadRequest(new { error = "Invalid JSON" });
            }
        });
    }
}
