using System;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Xna.Framework;

namespace TestGame
{
    public static class Program
    {
        // `--smoke` checks that the managed side starts on the target runtime
        // without needing a display: runtime, MonoGame, FMOD and Steam bindings.
        public static int Main(string[] args)
        {
            Console.WriteLine("runtime: " + RuntimeInformation.FrameworkDescription);
            Console.WriteLine("arch: " + RuntimeInformation.ProcessArchitecture);
            Console.WriteLine("base: " + AppContext.BaseDirectory);
            Console.WriteLine("monogame: " + typeof(Game).Assembly.GetName().Version);
            Console.WriteLine("fmod header: 0x" + FMOD.VERSION.number.ToString("x8"));
            Console.WriteLine("content: " + File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "Content", "hello.txt")).Trim());
            Console.WriteLine("culture: " + System.Globalization.CultureInfo.CurrentCulture.Name + " " + 1234.5.ToString("N1"));

            foreach (var lib in new[] { "libSDL2-2.0.so.0", "libopenal.so.1", "libfmod.so", "libsteam_api.so" })
            {
                bool ok = NativeLibrary.TryLoad(lib, typeof(Program).Assembly, null, out var handle);
                Console.WriteLine("native " + lib + ": " + (ok ? "loaded" : "missing"));
            }

            try
            {
                Console.WriteLine("steam restart needed: " + Steamworks.SteamAPI.RestartAppIfNecessary(new Steamworks.AppId_t(597760)));
                bool steam = Steamworks.SteamAPI.Init();
                Console.WriteLine("steam init: " + steam);
                if (steam)
                {
                    // What a game typically does once Steam is up.
                    Console.WriteLine("steam language: " + Steamworks.SteamApps.GetCurrentGameLanguage());
                    Console.WriteLine("steam persona: " + Steamworks.SteamFriends.GetPersonaName());
                    Console.WriteLine("steam user: " + Steamworks.SteamUser.GetSteamID());
                    Console.WriteLine("steam stats: " + Steamworks.SteamUserStats.RequestCurrentStats());
                    Console.WriteLine("steam achievement: " + Steamworks.SteamUserStats.SetAchievement("ACH_TEST"));
                    Steamworks.SteamAPI.RunCallbacks();
                    Steamworks.SteamAPI.Shutdown();
                }
            }
            catch (Exception e)
            {
                Console.WriteLine("steam init: " + e.GetType().Name);
            }

            if (Array.IndexOf(args, "--smoke") >= 0)
            {
                Console.WriteLine("SMOKE OK");
                return 0;
            }
            using (var game = new TestGame())
                game.Run();
            return 0;
        }
    }

    public class TestGame : Game
    {
        private readonly GraphicsDeviceManager _graphics;
        private int _frames;

        public TestGame()
        {
            _graphics = new GraphicsDeviceManager(this);
        }

        protected override void Update(GameTime gameTime)
        {
            if (++_frames > 120)
                Exit();
            base.Update(gameTime);
        }

        protected override void Draw(GameTime gameTime)
        {
            GraphicsDevice.Clear(Color.CornflowerBlue);
            base.Draw(gameTime);
        }
    }
}

namespace FMOD
{
    // Same shape as the FMOD 2.02 C# wrapper (fmod.cs) that games embed.
    public partial class VERSION
    {
        public const int number = 0x00020218;
        public const string dll = "fmod";
    }

    public static class Native
    {
        [DllImport(VERSION.dll, EntryPoint = "FMOD5_System_Create")]
        public static extern int System_Create(out IntPtr system, uint headerversion);
    }
}
