using System.Net;
using System.Net.Mail;
using AdoAutopilot.Models;
using Microsoft.Extensions.Options;

namespace AdoAutopilot.Notifications;

public class EmailNotifier : INotificationChannel
{
    private readonly AutopilotConfig _config;
    private readonly ILogger<EmailNotifier> _logger;

    public string Name => "Email (SMTP)";
    public bool IsEnabled => !string.IsNullOrEmpty(_config.SmtpHost)
                          && !string.IsNullOrEmpty(_config.EmailTo);

    public EmailNotifier(IOptions<AutopilotConfig> config, ILogger<EmailNotifier> logger)
    {
        _config = config.Value;
        _logger = logger;
    }

    public async Task SendAsync(NotificationMessage message, CancellationToken ct = default)
    {
        if (!IsEnabled) return;

        try
        {
            using var smtp = new SmtpClient(_config.SmtpHost, _config.SmtpPort)
            {
                EnableSsl = _config.SmtpPort != 25,
                Credentials = !string.IsNullOrEmpty(_config.SmtpUser)
                    ? new NetworkCredential(_config.SmtpUser, _config.SmtpPassword)
                    : null
            };

            var from = !string.IsNullOrEmpty(_config.EmailFrom) ? _config.EmailFrom : "autopilot@noreply.local";
            var mail = new MailMessage(from, _config.EmailTo)
            {
                Subject = $"[Autopilot] {message.Title}",
                Body = message.Summary,
                IsBodyHtml = false
            };

            await smtp.SendMailAsync(mail, ct);
            _logger.LogDebug("Email sent: {Title}", message.Title);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Email notification failed");
        }
    }
}
