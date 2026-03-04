using AdoAutopilot.Data.Entities;
using AdoAutopilot.Models;
using Microsoft.EntityFrameworkCore;
using System.Text.Json;

namespace AdoAutopilot.Data;

public class ExecutionRepository
{
    private readonly IDbContextFactory<AutopilotDbContext> _dbFactory;

    public ExecutionRepository(IDbContextFactory<AutopilotDbContext> dbFactory) => _dbFactory = dbFactory;

    public async Task<ExecutionRecord> StartExecutionAsync(WorkItemInfo item, string skill)
    {
        await using var db = await _dbFactory.CreateDbContextAsync();
        var record = new ExecutionRecord
        {
            WorkItemId = item.Id,
            Title = item.Title,
            Category = item.Category.ToString(),
            SkillUsed = skill,
            Status = ExecutionStatus.Running,
            StartedAt = DateTime.UtcNow
        };
        db.Executions.Add(record);
        await db.SaveChangesAsync();
        return record;
    }

    public async Task CompleteExecutionAsync(int recordId, ExecutionResult result)
    {
        await using var db = await _dbFactory.CreateDbContextAsync();
        var record = await db.Executions.FindAsync(recordId);
        if (record == null) return;

        record.Status = result.Success ? ExecutionStatus.Success : ExecutionStatus.Failed;
        record.BranchName = result.BranchName;
        record.PrUrl = result.PrUrl;
        record.FilesChanged = result.FilesChanged.Count > 0
            ? JsonSerializer.Serialize(result.FilesChanged) : null;
        record.Error = result.Error?[..Math.Min(result.Error.Length, 2000)];
        record.Output = result.Output.Length > 5000 ? result.Output[..5000] : result.Output;
        record.DurationSeconds = result.Duration.TotalSeconds;
        record.CompletedAt = DateTime.UtcNow;

        await db.SaveChangesAsync();
    }

    public async Task MarkRetryingAsync(int recordId, int retryCount)
    {
        await using var db = await _dbFactory.CreateDbContextAsync();
        var record = await db.Executions.FindAsync(recordId);
        if (record == null) return;
        record.Status = ExecutionStatus.Retrying;
        record.RetryCount = retryCount;
        await db.SaveChangesAsync();
    }

    public async Task<List<ExecutionRecord>> GetRecentAsync(int count = 50)
    {
        await using var db = await _dbFactory.CreateDbContextAsync();
        return await db.Executions
            .OrderByDescending(e => e.StartedAt)
            .Take(count)
            .AsNoTracking()
            .ToListAsync();
    }

    public async Task<List<ExecutionRecord>> GetByWorkItemAsync(int workItemId)
    {
        await using var db = await _dbFactory.CreateDbContextAsync();
        return await db.Executions
            .Where(e => e.WorkItemId == workItemId)
            .OrderByDescending(e => e.StartedAt)
            .AsNoTracking()
            .ToListAsync();
    }

    public async Task<(int total, int success, int failed, double avgDuration)> GetStatsAsync(DateTime? since = null)
    {
        await using var db = await _dbFactory.CreateDbContextAsync();
        var query = db.Executions.AsNoTracking();
        if (since.HasValue) query = query.Where(e => e.StartedAt >= since.Value);

        var total = await query.CountAsync();
        var success = await query.CountAsync(e => e.Status == ExecutionStatus.Success);
        var failed = await query.CountAsync(e => e.Status == ExecutionStatus.Failed);
        var avgDuration = total > 0 ? await query.AverageAsync(e => e.DurationSeconds) : 0;

        return (total, success, failed, avgDuration);
    }

    public async Task UpdateCostAsync(int recordId, long tokens)
    {
        await using var db = await _dbFactory.CreateDbContextAsync();
        var record = await db.Executions.FindAsync(recordId);
        if (record == null) return;
        record.CostTokens = tokens;
        await db.SaveChangesAsync();
    }
}
