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

            string video = Environment.GetEnvironmentVariable("YP_TEST_VIDEO");
            if (!string.IsNullOrEmpty(video))
                Console.WriteLine(Theorafile.TF.Decode(video));

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

namespace Theorafile
{
    // FuncLoader-style binding like the one in the game's MonoGame build:
    // the library is dlopen()ed by name and functions bound to delegates.
    public static class TF
    {
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate int d_tf_fopen(IntPtr fname, IntPtr file);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void d_tf_videoinfo(IntPtr file, out int width, out int height, out double fps, out int fmt);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate int d_tf_readvideo(IntPtr file, IntPtr buffer, int numframes);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate int d_tf_eos(IntPtr file);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void d_tf_close(IntPtr file);

        private static T Load<T>(IntPtr lib, string name) =>
            Marshal.GetDelegateForFunctionPointer<T>(NativeLibrary.GetExport(lib, name));

        public static string Decode(string path)
        {
            IntPtr lib = NativeLibrary.Load("libtheorafile.so");
            var fopen = Load<d_tf_fopen>(lib, "tf_fopen");
            var videoinfo = Load<d_tf_videoinfo>(lib, "tf_videoinfo");
            var readvideo = Load<d_tf_readvideo>(lib, "tf_readvideo");
            var eos = Load<d_tf_eos>(lib, "tf_eos");
            var close = Load<d_tf_close>(lib, "tf_close");

            IntPtr file = Marshal.AllocHGlobal(1472); // Theorafile-CS size64Unix
            IntPtr name = Marshal.StringToCoTaskMemUTF8(path);
            try
            {
                if (fopen(name, file) < 0)
                    return "video: open failed";
                videoinfo(file, out int w, out int h, out double fps, out int fmt);
                IntPtr buf = Marshal.AllocHGlobal(w * h * 3);
                int frames = 0;
                while (eos(file) == 0 && frames < 10000)
                {
                    readvideo(file, buf, 1);
                    frames++;
                }
                Marshal.FreeHGlobal(buf);
                close(file);
                return "video decoded: " + w + "x" + h + " fmt " + fmt + ", " + frames + " frames";
            }
            finally
            {
                Marshal.FreeCoTaskMem(name);
                Marshal.FreeHGlobal(file);
            }
        }
    }
}
