using AdoAutopilot.Data.Entities;
using Microsoft.EntityFrameworkCore;

namespace AdoAutopilot.Data;

public class AutopilotDbContext : DbContext
{
    public AutopilotDbContext(DbContextOptions<AutopilotDbContext> options) : base(options) { }

    public DbSet<ExecutionRecord> Executions => Set<ExecutionRecord>();

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        modelBuilder.Entity<ExecutionRecord>(e =>
        {
            e.HasKey(x => x.Id);
            e.Property(x => x.Title).HasMaxLength(500);
            e.Property(x => x.Category).HasMaxLength(50);
            e.Property(x => x.SkillUsed).HasMaxLength(200);
            e.Property(x => x.BranchName).HasMaxLength(200);
            e.Property(x => x.PrUrl).HasMaxLength(500);
            e.Property(x => x.Error).HasMaxLength(2000);
            e.Property(x => x.Output).HasMaxLength(5000);
            e.Property(x => x.Status).HasConversion<string>().HasMaxLength(20);
            e.HasIndex(x => x.WorkItemId);
            e.HasIndex(x => x.StartedAt);
            e.HasIndex(x => x.Status);
        });
    }
}
