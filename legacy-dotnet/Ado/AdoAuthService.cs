using System.Diagnostics;
using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using AdoAutopilot.Models;
using Microsoft.Extensions.Options;

namespace AdoAutopilot.Ado;

/// <summary>
/// Azure DevOps OAuth 2.0 Authorization Code flow via browser redirect.
/// First run → opens browser → user logs in → token stored locally.
/// Subsequent runs → auto-refresh from stored token.
/// </summary>
public class AdoAuthService
{
    private readonly AutopilotConfig _config;
    private readonly ILogger<AdoAuthService> _logger;
    private readonly string _tokenFilePath;
    private TokenInfo? _token;

    private const string AuthUrl = "https://app.vssps.visualstudio.com/oauth2/authorize";
    private const string TokenUrl = "https://app.vssps.visualstudio.com/oauth2/token";
    private const string DefaultScope = "vso.work_write vso.code_write";

    public AdoAuthService(IOptions<AutopilotConfig> config, ILogger<AdoAuthService> logger)
    {
        _config = config.Value;
        _logger = logger;
        _tokenFilePath = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            ".ado-autopilot-token.json");
    }

    /// <summary>
    /// Get a valid access token. Will trigger browser login if needed.
    /// </summary>
    public async Task<string> GetAccessTokenAsync(CancellationToken ct = default)
    {
        // 1. If PAT is configured, use it directly (backward compat)
        if (!string.IsNullOrEmpty(_config.AdoPat))
        {
            return _config.AdoPat;
        }

        // 2. Try load saved token
        _token ??= await LoadTokenAsync();

        // 3. If token exists and not expired, return it
        if (_token != null && !_token.IsExpired)
        {
            return _token.AccessToken;
        }

        // 4. If token exists but expired, try refresh
        if (_token?.RefreshToken != null)
        {
            var refreshed = await RefreshTokenAsync(_token.RefreshToken, ct);
            if (refreshed != null)
            {
                _token = refreshed;
                await SaveTokenAsync(_token);
                return _token.AccessToken;
            }
        }

        // 5. No token or refresh failed → browser login
        if (string.IsNullOrEmpty(_config.OAuthAppId) || string.IsNullOrEmpty(_config.OAuthAppSecret))
        {
            throw new InvalidOperationException(
                "No PAT and no OAuth app configured. Set either:\n" +
                "  - Autopilot:AdoPat (Personal Access Token)\n" +
                "  - Autopilot:OAuthAppId + OAuthAppSecret (OAuth app from https://app.vssps.visualstudio.com/app/register)");
        }

        _token = await BrowserLoginAsync(ct);
        await SaveTokenAsync(_token);
        return _token.AccessToken;
    }

    /// <summary>
    /// Get Authorization header value based on auth method.
    /// </summary>
    public async Task<AuthenticationHeaderValue> GetAuthHeaderAsync(CancellationToken ct = default)
    {
        if (!string.IsNullOrEmpty(_config.AdoPat))
        {
            // PAT uses Basic auth
            var encoded = Convert.ToBase64String(Encoding.ASCII.GetBytes($":{_config.AdoPat}"));
            return new AuthenticationHeaderValue("Basic", encoded);
        }

        // OAuth uses Bearer
        var token = await GetAccessTokenAsync(ct);
        return new AuthenticationHeaderValue("Bearer", token);
    }

    /// <summary>
    /// Open browser, start local HTTP listener, wait for redirect with auth code.
    /// </summary>
    private async Task<TokenInfo> BrowserLoginAsync(CancellationToken ct)
    {
        // Find a free port
        var listener = new HttpListener();
        var port = FindFreePort();
        var redirectUri = $"http://localhost:{port}/callback";
        listener.Prefixes.Add($"http://localhost:{port}/");
        listener.Start();

        // Build auth URL
        var state = Guid.NewGuid().ToString("N");
        var authUri = $"{AuthUrl}?client_id={_config.OAuthAppId}" +
                      $"&response_type=Assertion" +
                      $"&state={state}" +
                      $"&scope={Uri.EscapeDataString(DefaultScope)}" +
                      $"&redirect_uri={Uri.EscapeDataString(redirectUri)}";

        _logger.LogInformation("Opening browser for Azure DevOps login...");
        _logger.LogInformation("If browser doesn't open, navigate to: {Url}", authUri);

        // Open browser
        OpenBrowser(authUri);

        Console.WriteLine();
        Console.WriteLine("  Waiting for browser login... (press Ctrl+C to cancel)");
        Console.WriteLine();

        // Wait for callback
        var context = await listener.GetContextAsync().WaitAsync(TimeSpan.FromMinutes(5), ct);
        var code = context.Request.QueryString["code"];
        var returnedState = context.Request.QueryString["state"];

        // Send response to browser
        var responseHtml = "<html><body><h2>Login successful!</h2><p>You can close this tab.</p></body></html>";
        var buffer = Encoding.UTF8.GetBytes(responseHtml);
        context.Response.ContentType = "text/html";
        context.Response.ContentLength64 = buffer.Length;
        await context.Response.OutputStream.WriteAsync(buffer, ct);
        context.Response.Close();
        listener.Stop();

        if (string.IsNullOrEmpty(code))
            throw new InvalidOperationException("No authorization code received from browser callback");

        if (returnedState != state)
            throw new InvalidOperationException("State mismatch — possible CSRF attack");

        _logger.LogInformation("Authorization code received, exchanging for token...");

        // Exchange code for token
        return await ExchangeCodeAsync(code, redirectUri, ct);
    }

    private async Task<TokenInfo> ExchangeCodeAsync(string code, string redirectUri, CancellationToken ct)
    {
        using var http = new HttpClient();
        var body = new FormUrlEncodedContent(new Dictionary<string, string>
        {
            ["client_assertion_type"] = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            ["client_assertion"] = _config.OAuthAppSecret,
            ["grant_type"] = "urn:ietf:params:oauth:grant-type:jwt-bearer",
            ["assertion"] = code,
            ["redirect_uri"] = redirectUri
        });

        var response = await http.PostAsync(TokenUrl, body, ct);
        var json = await response.Content.ReadAsStringAsync(ct);

        if (!response.IsSuccessStatusCode)
            throw new HttpRequestException($"Token exchange failed: {response.StatusCode}\n{json}");

        var tokenResponse = JsonSerializer.Deserialize<OAuthTokenResponse>(json)
            ?? throw new InvalidOperationException("Failed to parse token response");

        return new TokenInfo
        {
            AccessToken = tokenResponse.AccessToken,
            RefreshToken = tokenResponse.RefreshToken,
            ExpiresAt = DateTime.UtcNow.AddSeconds(tokenResponse.ExpiresIn - 60) // 60s buffer
        };
    }

    private async Task<TokenInfo?> RefreshTokenAsync(string refreshToken, CancellationToken ct)
    {
        try
        {
            using var http = new HttpClient();
            var body = new FormUrlEncodedContent(new Dictionary<string, string>
            {
                ["client_assertion_type"] = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                ["client_assertion"] = _config.OAuthAppSecret,
                ["grant_type"] = "refresh_token",
                ["assertion"] = refreshToken,
                ["redirect_uri"] = $"http://localhost:0/callback" // Not used for refresh, but required
            });

            var response = await http.PostAsync(TokenUrl, body, ct);
            if (!response.IsSuccessStatusCode)
            {
                _logger.LogWarning("Token refresh failed: {Status}", response.StatusCode);
                return null;
            }

            var json = await response.Content.ReadAsStringAsync(ct);
            var tokenResponse = JsonSerializer.Deserialize<OAuthTokenResponse>(json);
            if (tokenResponse == null) return null;

            _logger.LogInformation("Token refreshed successfully");
            return new TokenInfo
            {
                AccessToken = tokenResponse.AccessToken,
                RefreshToken = tokenResponse.RefreshToken,
                ExpiresAt = DateTime.UtcNow.AddSeconds(tokenResponse.ExpiresIn - 60)
            };
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Token refresh failed");
            return null;
        }
    }

    private async Task<TokenInfo?> LoadTokenAsync()
    {
        if (!File.Exists(_tokenFilePath)) return null;
        try
        {
            var json = await File.ReadAllTextAsync(_tokenFilePath);
            return JsonSerializer.Deserialize<TokenInfo>(json);
        }
        catch
        {
            return null;
        }
    }

    private async Task SaveTokenAsync(TokenInfo token)
    {
        var json = JsonSerializer.Serialize(token, new JsonSerializerOptions { WriteIndented = true });
        await File.WriteAllTextAsync(_tokenFilePath, json);
        _logger.LogDebug("Token saved to {Path}", _tokenFilePath);
    }

    private static void OpenBrowser(string url)
    {
        try
        {
            if (OperatingSystem.IsWindows())
                Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
            else if (OperatingSystem.IsMacOS())
                Process.Start("open", url);
            else
                Process.Start("xdg-open", url);
        }
        catch { /* Browser open is best-effort */ }
    }

    private static int FindFreePort()
    {
        var listener = new System.Net.Sockets.TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }
}

public class TokenInfo
{
    public string AccessToken { get; set; } = string.Empty;
    public string? RefreshToken { get; set; }
    public DateTime ExpiresAt { get; set; }

    [JsonIgnore]
    public bool IsExpired => DateTime.UtcNow >= ExpiresAt;
}

internal class OAuthTokenResponse
{
    [JsonPropertyName("access_token")]
    public string AccessToken { get; set; } = string.Empty;

    [JsonPropertyName("refresh_token")]
    public string RefreshToken { get; set; } = string.Empty;

    [JsonPropertyName("expires_in")]
    public int ExpiresIn { get; set; }

    [JsonPropertyName("token_type")]
    public string TokenType { get; set; } = string.Empty;
}
