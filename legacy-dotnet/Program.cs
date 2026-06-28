using AdoAutopilot.Ado;
using AdoAutopilot.Api;
using AdoAutopilot.Data;
using AdoAutopilot.Execution;
using AdoAutopilot.HealthChecks;
using AdoAutopilot.Models;
using AdoAutopilot.Notifications;
using AdoAutopilot.MultiTenant;
using AdoAutopilot.Security;
using AdoAutopilot.Plugins;
using AdoAutopilot.Routing;
using AdoAutopilot.Scheduling;
using AdoAutopilot.Tracking;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Diagnostics.HealthChecks;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Diagnostics.HealthChecks;
using Serilog;
using System.Text.Json;

Log.Logger = new LoggerConfiguration()
    .WriteTo.Console(outputTemplate: "[{Timestamp:HH:mm:ss} {Level:u3}] {Message:lj} {Properties:j}{NewLine}{Exception}")
    .WriteTo.File("logs/autopilot-.log",
        rollingInterval: RollingInterval.Day,
        outputTemplate: "{Timestamp:yyyy-MM-dd HH:mm:ss.fff} [{Level:u3}] {Message:lj} {Properties:j}{NewLine}{Exception}",
        retainedFileCountLimit: 30)
    .Enrich.FromLogContext()
    .Enrich.WithProperty("MachineName", Environment.MachineName)
    .MinimumLevel.Information()
    .MinimumLevel.Override("Microsoft", Serilog.Events.LogEventLevel.Warning)
    .MinimumLevel.Override("AdoAutopilot", Serilog.Events.LogEventLevel.Debug)
    .CreateLogger();

var builder = WebApplication.CreateBuilder(args);
builder.Host.UseSerilog();

// Config
builder.Services.Configure<AutopilotConfig>(
    builder.Configuration.GetSection(AutopilotConfig.SectionName));

var config = builder.Configuration.GetSection(AutopilotConfig.SectionName).Get<AutopilotConfig>() ?? new();

// Kestrel — health check port
builder.WebHost.ConfigureKestrel(k => k.ListenAnyIP(config.HealthPort));

// SQLite DB
builder.Services.AddDbContextFactory<AutopilotDbContext>(opt =>
    opt.UseSqlite("Data Source=autopilot.db"));
builder.Services.AddSingleton<ExecutionRepository>();

// Services
builder.Services.AddSingleton(sp =>
    new RetryPolicy(config.MaxRetries, config.RetryBackoffSeconds,
        sp.GetRequiredService<ILogger<RetryPolicy>>()));
builder.Services.AddSingleton<AdoAuthService>();
builder.Services.AddHttpClient<AdoClient>();
builder.Services.AddSingleton<TaskRouter>();
builder.Services.AddSingleton<ClaudeExecutor>();
builder.Services.AddSingleton<AutoReviewer>();
builder.Services.AddSingleton<ScheduleGuard>();
builder.Services.AddSingleton<AdoNotifier>();
builder.Services.AddHttpClient();
builder.Services.AddSingleton<INotificationChannel, TeamsNotifier>();
builder.Services.AddSingleton<INotificationChannel, ZaloNotifier>();
builder.Services.AddSingleton<INotificationChannel, EmailNotifier>();

// Health checks
builder.Services.AddHealthChecks()
    .AddCheck<AdoHealthCheck>("ado", tags: new[] { "ready" })
    .AddCheck<ClaudeHealthCheck>("claude", tags: new[] { "ready" })
    .AddCheck<DiskSpaceHealthCheck>("disk", tags: new[] { "ready" });

builder.Services.AddSingleton<FeedbackHandler>();
builder.Services.AddSingleton<CostTracker>();
builder.Services.AddSingleton<RequirementDecomposer>();
builder.Services.AddSingleton<PluginManager>();
builder.Services.AddSingleton<TenantManager>();
builder.Services.AddSingleton<RbacPolicy>();

// Blazor Dashboard
builder.Services.AddRazorPages().AddRazorPagesOptions(options =>
{
    options.RootDirectory = "/";
});
builder.Services.AddServerSideBlazor();

// Background services
builder.Services.AddHostedService<AdoPollerService>();
builder.Services.AddHostedService<PrMonitorService>();

var app = builder.Build();

// Health endpoint
app.MapHealthChecks("/health", new HealthCheckOptions
{
    ResponseWriter = async (context, report) =>
    {
        context.Response.ContentType = "application/json";
        var result = new
        {
            status = report.Status.ToString(),
            duration = report.TotalDuration.ToString(),
            checks = report.Entries.Select(e => new
            {
                name = e.Key,
                status = e.Value.Status.ToString(),
                description = e.Value.Description,
                duration = e.Value.Duration.ToString()
            })
        };
        await context.Response.WriteAsync(JsonSerializer.Serialize(result,
            new JsonSerializerOptions { WriteIndented = true }));
    }
});

// Static files + Routing
app.UseStaticFiles();
app.UseRouting();
app.MapGet("/metrics", async ctx =>
{
    ctx.Response.ContentType = "text/plain; version=0.0.4; charset=utf-8";
    await Prometheus.Metrics.DefaultRegistry.CollectAndExportAsTextAsync(ctx.Response.Body);
});

// Webhook endpoint (ADO Service Hook)
app.MapWebhookEndpoints();

// Blazor Dashboard
app.MapBlazorHub();
app.MapFallbackToPage("/dashboard/{**segment}", "/Dashboard/Pages/_Host");

// Load plugins
var pluginMgr = app.Services.GetRequiredService<PluginManager>();
await pluginMgr.LoadAndInitAsync(config.PluginsDirectory, app.Services);

// Auto-migrate SQLite on startup
using (var scope = app.Services.CreateScope())
{
    var dbFactory = scope.ServiceProvider.GetRequiredService<IDbContextFactory<AutopilotDbContext>>();
    await using var db = await dbFactory.CreateDbContextAsync();
    await db.Database.EnsureCreatedAsync();
}

app.Run();
