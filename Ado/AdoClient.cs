using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using AdoAutopilot.Models;
using Microsoft.Extensions.Options;

namespace AdoAutopilot.Ado;

/// <summary>
/// Direct ADO REST API client (not MCP — runs outside Claude Code).
/// </summary>
public class AdoClient
{
    private readonly HttpClient _http;
    private readonly AdoAuthService _auth;
    private readonly AutopilotConfig _config;
    private readonly ILogger<AdoClient> _logger;

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNameCaseInsensitive = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull
    };

    public AdoClient(HttpClient http, AdoAuthService auth, IOptions<AutopilotConfig> config, ILogger<AdoClient> logger)
    {
        _http = http;
        _auth = auth;
        _config = config.Value;
        _logger = logger;

        var baseUrl = _config.AdoOrganization.TrimEnd('/');
        _http.BaseAddress = new Uri($"{baseUrl}/");
    }

    /// <summary>
    /// Set auth header before each request (supports PAT + OAuth refresh).
    /// </summary>
    private async Task EnsureAuthAsync(CancellationToken ct)
    {
        _http.DefaultRequestHeaders.Authorization = await _auth.GetAuthHeaderAsync(ct);
    }

    /// <summary>
    /// Query work items tagged with the trigger tag (e.g. "autopilot") in pending states.
    /// </summary>
    public async Task<List<WorkItemInfo>> GetPendingWorkItemsAsync(CancellationToken ct = default)
    {
        var wiql = $"""
            SELECT [System.Id]
            FROM WorkItems
            WHERE [System.Tags] CONTAINS '{_config.TriggerTag}'
              AND [System.State] IN ('New', 'To Do', 'Proposed')
              AND [System.TeamProject] = '{_config.AdoProject}'
            ORDER BY [System.ChangedDate] DESC
            """;

        var body = new { query = wiql };
        await EnsureAuthAsync(ct);
        var response = await _http.PostAsJsonAsync(
            $"{_config.AdoProject}/_apis/wit/wiql?api-version=7.1", body, JsonOpts, ct);

        var responseBody = await response.Content.ReadAsStringAsync(ct);
        if (!response.IsSuccessStatusCode || !responseBody.TrimStart().StartsWith('{'))
        {
            _logger.LogWarning("WIQL query failed: {Status} (response is not JSON — check PAT)", response.StatusCode);
            return new();
        }

        _logger.LogDebug("WIQL response: {Body}", responseBody);
        var wiqlResult = JsonSerializer.Deserialize<WiqlResponse>(responseBody, JsonOpts);
        _logger.LogDebug("Parsed {Count} work item refs", wiqlResult?.WorkItems?.Count ?? -1);
        if (wiqlResult?.WorkItems == null || wiqlResult.WorkItems.Count == 0)
            return new();

        // Fetch full details in batches of 200
        var ids = wiqlResult.WorkItems.Select(w => w.Id).ToList();
        return await GetWorkItemsByIdsAsync(ids, ct);
    }

    /// <summary>
    /// Get full work item details by IDs.
    /// </summary>
    public async Task<List<WorkItemInfo>> GetWorkItemsByIdsAsync(List<int> ids, CancellationToken ct = default)
    {
        if (ids.Count == 0) return new();

        var fields = "System.Id,System.Title,System.WorkItemType,System.State," +
                     "System.AssignedTo,System.Description,Microsoft.VSTS.Common.AcceptanceCriteria," +
                     "System.Parent,System.Tags,System.AreaPath,System.IterationPath,System.ChangedDate," +
                     "Microsoft.VSTS.Common.Priority,System.CreatedBy";

        var idsParam = string.Join(",", ids.Take(200));
        var url = $"{_config.AdoProject}/_apis/wit/workitems?ids={idsParam}&fields={fields}&api-version=7.1";

        await EnsureAuthAsync(ct);
        var response = await _http.GetAsync(url, ct);
        if (!response.IsSuccessStatusCode)
        {
            _logger.LogWarning("GetWorkItems failed: {Status}", response.StatusCode);
            return new();
        }

        var result = await response.Content.ReadFromJsonAsync<WorkItemBatchResponse>(JsonOpts, ct);
        return result?.Value?.Select(MapToWorkItemInfo).ToList() ?? new();
    }

    /// <summary>
    /// Get a single work item by ID.
    /// </summary>
    public async Task<WorkItemInfo?> GetWorkItemAsync(int id, CancellationToken ct = default)
    {
        var items = await GetWorkItemsByIdsAsync(new List<int> { id }, ct);
        return items.FirstOrDefault();
    }

    /// <summary>
    /// Add a comment to a work item.
    /// </summary>
    public async Task<bool> AddCommentAsync(int workItemId, string comment, CancellationToken ct = default)
    {
        var body = new { text = comment };
        await EnsureAuthAsync(ct);
        var response = await _http.PostAsJsonAsync(
            $"{_config.AdoProject}/_apis/wit/workitems/{workItemId}/comments?api-version=7.1-preview.4",
            body, JsonOpts, ct);

        if (!response.IsSuccessStatusCode)
            _logger.LogWarning("AddComment failed for #{Id}: {Status}", workItemId, response.StatusCode);

        return response.IsSuccessStatusCode;
    }

    /// <summary>
    /// Update work item state.
    /// </summary>
    public async Task<bool> UpdateStateAsync(int workItemId, string newState, CancellationToken ct = default)
    {
        var patch = new[]
        {
            new { op = "replace", path = "/fields/System.State", value = (object)newState }
        };

        var content = new StringContent(
            JsonSerializer.Serialize(patch, JsonOpts),
            Encoding.UTF8,
            "application/json-patch+json");

        await EnsureAuthAsync(ct);
        var response = await _http.PatchAsync(
            $"{_config.AdoProject}/_apis/wit/workitems/{workItemId}?api-version=7.1",
            content, ct);

        if (!response.IsSuccessStatusCode)
            _logger.LogWarning("UpdateState failed for #{Id}: {Status}", workItemId, response.StatusCode);

        return response.IsSuccessStatusCode;
    }

    /// <summary>
    /// Add a tag to a work item (appends to existing tags).
    /// </summary>
    public async Task<bool> AddTagAsync(int workItemId, string tag, CancellationToken ct = default)
    {
        // First get current tags
        var item = await GetWorkItemAsync(workItemId, ct);
        if (item == null) return false;

        var currentTags = string.Join("; ", item.Tags);
        var newTags = string.IsNullOrEmpty(currentTags) ? tag : $"{currentTags}; {tag}";

        var patch = new[]
        {
            new { op = "replace", path = "/fields/System.Tags", value = (object)newTags }
        };

        var content = new StringContent(
            JsonSerializer.Serialize(patch, JsonOpts),
            Encoding.UTF8,
            "application/json-patch+json");

        await EnsureAuthAsync(ct);
        var response = await _http.PatchAsync(
            $"{_config.AdoProject}/_apis/wit/workitems/{workItemId}?api-version=7.1",
            content, ct);

        return response.IsSuccessStatusCode;
    }

    public async Task<int> CreateWorkItemAsync(string title, string type, int? parentId, string tag, CancellationToken ct = default)
    {
        var patch = new List<object>
        {
            new { op = "add", path = "/fields/System.Title", value = (object)title },
            new { op = "add", path = "/fields/System.Tags", value = (object)tag }
        };

        if (parentId.HasValue)
        {
            patch.Add(new
            {
                op = "add",
                path = "/relations/-",
                value = new
                {
                    rel = "System.LinkTypes.Hierarchy-Reverse",
                    url = $"{_config.AdoOrganization.TrimEnd('/')}/{_config.AdoProject}/_apis/wit/workitems/{parentId.Value}"
                }
            });
        }

        var content = new StringContent(
            JsonSerializer.Serialize(patch, JsonOpts),
            Encoding.UTF8,
            "application/json-patch+json");

        await EnsureAuthAsync(ct);
        var urlType = Uri.EscapeDataString(type);
        var response = await _http.PostAsync(
            $"{_config.AdoProject}/_apis/wit/workitems/${urlType}?api-version=7.1",
            content, ct);

        if (!response.IsSuccessStatusCode)
        {
            var body = await response.Content.ReadAsStringAsync(ct);
            _logger.LogWarning("CreateWorkItem failed: {Status} {Body}", response.StatusCode, body);
            return 0;
        }

        var result = await response.Content.ReadFromJsonAsync<JsonElement>(JsonOpts, ct);
        return result.TryGetProperty("id", out var id) ? id.GetInt32() : 0;
    }

    private static WorkItemInfo MapToWorkItemInfo(AdoWorkItem wi)
    {
        var f = wi.Fields;
        var assignedTo = f.TryGetValue("System.AssignedTo", out var ato)
            ? ato is JsonElement je && je.ValueKind == JsonValueKind.Object
                ? je.GetProperty("displayName").GetString()
                : ato?.ToString()
            : null;

        return new WorkItemInfo
        {
            Id = wi.Id,
            Title = GetString(f, "System.Title"),
            WorkItemType = GetString(f, "System.WorkItemType"),
            State = GetString(f, "System.State"),
            AssignedTo = assignedTo,
            Description = GetString(f, "System.Description"),
            AcceptanceCriteria = GetString(f, "Microsoft.VSTS.Common.AcceptanceCriteria"),
            ParentId = GetInt(f, "System.Parent"),
            Tags = GetString(f, "System.Tags")
                .Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                .ToList(),
            AreaPath = GetString(f, "System.AreaPath"),
            IterationPath = GetString(f, "System.IterationPath"),
            ChangedDate = GetDateTime(f, "System.ChangedDate"),
            Priority = GetInt(f, "Microsoft.VSTS.Common.Priority") ?? 3,
            CreatedBy = GetCreatedBy(f)
        };
    }

    private static string GetString(Dictionary<string, object?> fields, string key)
    {
        if (!fields.TryGetValue(key, out var v) || v == null) return string.Empty;
        return v is JsonElement je ? je.GetString() ?? string.Empty : v.ToString() ?? string.Empty;
    }

    private static int? GetInt(Dictionary<string, object?> fields, string key)
    {
        if (!fields.TryGetValue(key, out var v) || v == null) return null;
        if (v is JsonElement je && je.ValueKind == JsonValueKind.Number) return je.GetInt32();
        return int.TryParse(v.ToString(), out var i) ? i : null;
    }

    private static string? GetCreatedBy(Dictionary<string, object?> fields)
    {
        if (!fields.TryGetValue("System.CreatedBy", out var v) || v == null) return null;
        if (v is JsonElement je && je.ValueKind == JsonValueKind.Object)
            return je.TryGetProperty("displayName", out var dn) ? dn.GetString() : je.ToString();
        return v.ToString();
    }

    private static DateTime GetDateTime(Dictionary<string, object?> fields, string key)
    {
        if (!fields.TryGetValue(key, out var v) || v == null) return DateTime.MinValue;
        if (v is JsonElement je) return je.TryGetDateTime(out var dt) ? dt : DateTime.MinValue;
        return DateTime.TryParse(v.ToString(), out var d) ? d : DateTime.MinValue;
    }
}

// ADO REST API response models
internal class WiqlResponse
{
    public List<WiqlWorkItemRef> WorkItems { get; set; } = new();
}

internal class WiqlWorkItemRef
{
    public int Id { get; set; }
}

internal class WorkItemBatchResponse
{
    public List<AdoWorkItem> Value { get; set; } = new();
}

internal class AdoWorkItem
{
    public int Id { get; set; }
    public Dictionary<string, object?> Fields { get; set; } = new();
}
