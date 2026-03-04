using System.Diagnostics;
using System.Text.Json;
using AdoAutopilot.Ado;
using AdoAutopilot.Models;
using Microsoft.Extensions.Options;

namespace AdoAutopilot.Routing;

public class RequirementDecomposer
{
    private readonly AdoClient _ado;
    private readonly AutopilotConfig _config;
    private readonly ILogger<RequirementDecomposer> _logger;

    public RequirementDecomposer(AdoClient ado, IOptions<AutopilotConfig> config, ILogger<RequirementDecomposer> logger)
    {
        _ado = ado;
        _config = config.Value;
        _logger = logger;
    }

    public async Task<List<int>> DecomposeAsync(WorkItemInfo parent, CancellationToken ct)
    {
        if (!_config.AutoDecompose)
        {
            _logger.LogDebug("Auto-decompose disabled, skipping #{Id}", parent.Id);
            return new();
        }

        _logger.LogInformation("Decomposing requirement #{Id}: {Title}", parent.Id, parent.Title);

        // Generate child task definitions
        var children = GenerateChildTasks(parent);

        var createdIds = new List<int>();
        foreach (var child in children)
        {
            var id = await _ado.CreateWorkItemAsync(child.Title, child.Type, parent.Id, _config.TriggerTag, ct);
            if (id > 0)
            {
                createdIds.Add(id);
                _logger.LogInformation("Created child #{ChildId}: {Title}", id, child.Title);
            }
        }

        if (createdIds.Count > 0)
        {
            var childLinks = string.Join(", ", createdIds.Select(id => $"#{id}"));
            await _ado.AddCommentAsync(parent.Id,
                $"<b>📋 Decomposed into {createdIds.Count} child tasks:</b> {childLinks}", ct);
        }

        return createdIds;
    }

    private static List<(string Title, string Type)> GenerateChildTasks(WorkItemInfo parent)
    {
        var title = parent.Title.Replace("[BE]", "").Replace("[FE]", "").Replace("[DB]", "").Trim();
        return new List<(string, string)>
        {
            ($"[BE] Implement {title} API", "Task"),
            ($"[FE] Implement {title} UI", "Task"),
            ($"[DB] Migration for {title}", "Task"),
            ($"[QC] Test cases for {title}", "Task")
        };
    }
}
