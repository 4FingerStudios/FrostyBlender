using System;
using System.Diagnostics;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace FrostyBlender.Services;

/// <summary>Optional "Log in with Discord": Discord's OAuth2 implicit grant (no client secret in the app) with
/// the narrowest scope, identify. The browser comes back to a one-shot page on 127.0.0.1, which hands the
/// token to the app; the app reads the user's name and avatar once and forgets the token.</summary>
public static class DiscordLogin
{
    public const int Port = 24568;
    public static string RedirectUri => $"http://127.0.0.1:{Port}/discord";

    public static async Task<DiscordUser> SignIn(string appId, CancellationToken cancel)
    {
        if (appId.Length == 0) throw new InvalidOperationException("Discord login isn't set up in this build.");
        var state = Convert.ToHexString(RandomNumberGenerator.GetBytes(16));
        var listener = new TcpListener(IPAddress.Loopback, Port);
        try { listener.Start(); }
        catch (SocketException) { throw new InvalidOperationException($"Port {Port} is in use; close the other login window first."); }
        try
        {
            var url = "https://discord.com/oauth2/authorize?response_type=token&scope=identify&prompt=consent" +
                      $"&client_id={appId}&state={state}&redirect_uri={Uri.EscapeDataString(RedirectUri)}";
            Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancel);
            timeout.CancelAfter(TimeSpan.FromMinutes(5));
            while (true)
            {
                using var client = await listener.AcceptTcpClientAsync(timeout.Token);
                var stream = client.GetStream();
                var target = await RequestTarget(stream, timeout.Token);
                if (target.StartsWith("/discord/done?"))
                {
                    var query = System.Web.HttpUtility.ParseQueryString(target[(target.IndexOf('?') + 1)..]);
                    if (query["error"] is { } error)
                    {
                        await Respond(stream, Page("Login cancelled", "You can close this tab and go back to FrostyBlender."));
                        throw new InvalidOperationException("Discord login was cancelled: " + error);
                    }
                    if (query["state"] != state || string.IsNullOrEmpty(query["access_token"]))
                    {
                        await Respond(stream, Page("Login failed", "That login link didn't come from this app. Try again from FrostyBlender."));
                        continue;
                    }
                    var user = await Me(query["access_token"]!, timeout.Token);
                    await Respond(stream, Page($"Hi {WebUtility.HtmlEncode(user.DisplayName)}!", "You're signed in. You can close this tab and go back to FrostyBlender."));
                    return user;
                }
                // Discord puts the token after '#', which only the browser sees: this page passes it on.
                await Respond(stream, Page("Signing in…", "One moment.", """
                    <script>
                    const p = new URLSearchParams(location.hash.slice(1) || location.search.slice(1));
                    location.replace('/discord/done?' + p.toString());
                    </script>
                    """));
            }
        }
        finally
        {
            listener.Stop();
        }
    }

    private static async Task<DiscordUser> Me(string token, CancellationToken cancel)
    {
        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
        using var request = new HttpRequestMessage(HttpMethod.Get, "https://discord.com/api/v10/users/@me");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        using var response = await http.SendAsync(request, cancel);
        response.EnsureSuccessStatusCode();
        var me = JsonNode.Parse(await response.Content.ReadAsStringAsync(cancel))!;
        return new DiscordUser
        {
            Id = me["id"]?.GetValue<string>() ?? "",
            Username = me["username"]?.GetValue<string>() ?? "",
            GlobalName = me["global_name"]?.GetValue<string>() ?? "",
            Avatar = me["avatar"]?.GetValue<string>() ?? "",
        };
    }

    private static async Task<string> RequestTarget(NetworkStream stream, CancellationToken cancel)
    {
        var buffer = new byte[8192];
        var read = 0;
        while (read < buffer.Length)
        {
            var n = await stream.ReadAsync(buffer.AsMemory(read), cancel);
            if (n == 0) break;
            read += n;
            if (Encoding.ASCII.GetString(buffer, 0, read).Contains("\r\n\r\n")) break;
        }
        var line = Encoding.ASCII.GetString(buffer, 0, read).Split("\r\n")[0].Split(' ');
        return line.Length >= 2 && line[0] == "GET" ? line[1] : "";
    }

    private static async Task Respond(NetworkStream stream, string html)
    {
        var body = Encoding.UTF8.GetBytes(html);
        var head = Encoding.ASCII.GetBytes("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n" +
                                           "Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\n" +
                                           $"Content-Length: {body.Length}\r\nConnection: close\r\n\r\n");
        await stream.WriteAsync(head);
        await stream.WriteAsync(body);
    }

    private static string Page(string title, string text, string script = "") => $$"""
        <!doctype html><html><head><meta charset="utf-8"><title>FrostyBlender</title>
        <style>body{background:#16181d;color:#e8eaed;font-family:Segoe UI,sans-serif;display:grid;place-items:center;height:100vh;margin:0}
        .card{background:#1f2229;border:1px solid #2c313a;border-radius:14px;padding:28px 36px;text-align:center}
        h1{font-size:22px;margin:0 0 8px;color:#7ed957}p{margin:0;color:#a9afb8}</style></head>
        <body><div class="card"><h1>{{title}}</h1><p>{{text}}</p></div>{{script}}</body></html>
        """;
}
