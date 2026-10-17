using System;
using System.Buffers.Binary;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Text;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace FrostyBlender.Services;

/// <summary>Discord Rich Presence ("Playing FrostyBlender") through the Discord client's local pipe
/// (\\.\pipe\discord-ipc-N): a handshake with the application ID, then SET_ACTIVITY frames. Nothing leaves
/// this PC except through the user's own Discord client, and nothing happens when Discord isn't running.</summary>
public sealed class DiscordPresence : IDisposable
{
    private readonly string _appId;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private NamedPipeClientStream? _pipe;
    private string _last = "";
    private DateTime _nextTry = DateTime.MinValue;
    private readonly long _started = DateTimeOffset.UtcNow.ToUnixTimeSeconds();

    /// <summary>Discord fetches the picture itself, so the app needs no art asset uploaded in the Developer Portal.</summary>
    private const string IconUrl = "https://raw.githubusercontent.com/4FingerStudios/FrostyBlender/main/app/FrostyBlender/Assets/icon.png";

    public DiscordPresence(string appId) => _appId = appId;

    public bool Enabled => _appId.Length > 0;

    /// <summary>Shows `details` / `state` on the user's profile; repeated calls with the same text do nothing.</summary>
    public async Task Update(string details, string state)
    {
        if (!Enabled) return;
        var activity = new JsonObject
        {
            ["details"] = Trim(details),
            ["state"] = Trim(state),
            ["timestamps"] = new JsonObject { ["start"] = _started },
            ["assets"] = new JsonObject { ["large_image"] = IconUrl, ["large_text"] = "FrostyBlender: PvZ to Blender" },
            ["buttons"] = new JsonArray(
                new JsonObject { ["label"] = "Get FrostyBlender", ["url"] = "https://github.com/4FingerStudios/FrostyBlender/releases" },
                new JsonObject { ["label"] = "Join the Discord", ["url"] = "https://discord.gg/Vh7cgGzUyB" }),
        };
        await Send(activity);
    }

    /// <summary>Removes the activity (when the user turns presence off).</summary>
    public Task Clear() => Send(null);

    private static string Trim(string text) => text.Length <= 2 ? text + "  " : text.Length > 120 ? text[..120] : text;

    private async Task Send(JsonObject? activity)
    {
        var key = activity?.ToJsonString() ?? "clear";
        if (key == _last) return;
        if (!await _gate.WaitAsync(0)) return;
        try
        {
            if (!await Connect()) return;
            var frame = new JsonObject
            {
                ["cmd"] = "SET_ACTIVITY",
                ["args"] = new JsonObject { ["pid"] = Environment.ProcessId, ["activity"] = activity },
                ["nonce"] = Guid.NewGuid().ToString(),
            };
            await Write(1, frame.ToJsonString());
            await Read();
            _last = key;
        }
        catch (Exception)
        {
            Drop();  // Discord closed or restarted: connect again next time
        }
        finally
        {
            _gate.Release();
        }
    }

    private async Task<bool> Connect()
    {
        if (_pipe is { IsConnected: true }) return true;
        if (DateTime.UtcNow < _nextTry) return false;
        _nextTry = DateTime.UtcNow.AddSeconds(15);
        for (var i = 0; i < 10; i++)
        {
            var pipe = new NamedPipeClientStream(".", $"discord-ipc-{i}", PipeDirection.InOut, PipeOptions.Asynchronous);
            try
            {
                await pipe.ConnectAsync(150);
                _pipe = pipe;
                await Write(0, new JsonObject { ["v"] = 1, ["client_id"] = _appId }.ToJsonString());
                var ready = await Read();
                if (ready.Contains("\"READY\"")) return true;
                Drop();
                return false;
            }
            catch (Exception)
            {
                pipe.Dispose();
                if (_pipe == pipe) _pipe = null;
            }
        }
        return false;
    }

    private async Task Write(int opcode, string json)
    {
        var body = Encoding.UTF8.GetBytes(json);
        var frame = new byte[8 + body.Length];
        BinaryPrimitives.WriteInt32LittleEndian(frame.AsSpan(0), opcode);
        BinaryPrimitives.WriteInt32LittleEndian(frame.AsSpan(4), body.Length);
        body.CopyTo(frame, 8);
        await _pipe!.WriteAsync(frame);
        await _pipe.FlushAsync();
    }

    private async Task<string> Read()
    {
        var header = new byte[8];
        await ReadExactly(header);
        var length = BinaryPrimitives.ReadInt32LittleEndian(header.AsSpan(4));
        if (length is < 0 or > 1 << 20) throw new InvalidDataException("Bad Discord frame");
        var body = new byte[length];
        await ReadExactly(body);
        return Encoding.UTF8.GetString(body);
    }

    private async Task ReadExactly(byte[] buffer)
    {
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(3));
        var read = 0;
        while (read < buffer.Length)
        {
            var n = await _pipe!.ReadAsync(buffer.AsMemory(read), timeout.Token);
            if (n == 0) throw new EndOfStreamException();
            read += n;
        }
    }

    private void Drop()
    {
        try { _pipe?.Dispose(); } catch (Exception) { }
        _pipe = null;
        _last = "";
    }

    public void Dispose() => Drop();
}
