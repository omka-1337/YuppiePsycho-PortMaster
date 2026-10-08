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

            int tt = Array.IndexOf(args, "--texture-test");
            if (tt >= 0 && tt + 2 < args.Length)
            {
                using (var g = new TextureTestGame(args[tt + 1], args[tt + 2]))
                    g.Run();
                return 0;
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

    // Loads Content/<name>.xnb through the ContentManager, draws it 1:1 into a
    // render target and dumps the RGBA pixels, to check converted textures
    // the way the game would see them.
    public class TextureTestGame : Game
    {
        private readonly string _name;
        private readonly string _dump;

        public TextureTestGame(string name, string dump)
        {
            new GraphicsDeviceManager(this);
            Content.RootDirectory = "Content";
            _name = name;
            _dump = dump;
        }

        protected override void LoadContent()
        {
            var tex = Content.Load<Microsoft.Xna.Framework.Graphics.Texture2D>(_name);
            var rt = new Microsoft.Xna.Framework.Graphics.RenderTarget2D(GraphicsDevice, tex.Width, tex.Height, false,
                Microsoft.Xna.Framework.Graphics.SurfaceFormat.Color, Microsoft.Xna.Framework.Graphics.DepthFormat.None);
            GraphicsDevice.SetRenderTarget(rt);
            GraphicsDevice.Clear(Color.Transparent);
            var batch = new Microsoft.Xna.Framework.Graphics.SpriteBatch(GraphicsDevice);
            batch.Begin(Microsoft.Xna.Framework.Graphics.SpriteSortMode.Immediate,
                Microsoft.Xna.Framework.Graphics.BlendState.Opaque, Microsoft.Xna.Framework.Graphics.SamplerState.PointClamp);
            batch.Draw(tex, Vector2.Zero, Color.White);
            batch.End();
            GraphicsDevice.SetRenderTarget(null);
            var pixels = new byte[tex.Width * tex.Height * 4];
            rt.GetData(pixels);
            File.WriteAllBytes(_dump, pixels);
            Console.WriteLine("texture-test: " + _name + " " + tex.Width + "x" + tex.Height + " " + tex.Format);
            Exit();
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

namespace Theorafile
{
    // Function-pointer style binding (delegate* unmanaged), the other common
    // way to bind a dlopen()ed library from modern C#.
    public static unsafe class TFPointers
    {
        private static delegate* unmanaged[Cdecl]<IntPtr, int*, int*, double*, int*, void> tf_videoinfo_ptr;

        public static void Bind(IntPtr lib)
        {
            tf_videoinfo_ptr = (delegate* unmanaged[Cdecl]<IntPtr, int*, int*, double*, int*, void>)
                NativeLibrary.GetExport(lib, "tf_videoinfo");
        }

        public static void VideoInfo(IntPtr file, out int width, out int height, out double fps, out int fmt)
        {
            int w, h, f;
            double r;
            tf_videoinfo_ptr(file, &w, &h, &r, &f);
            width = w; height = h; fps = r; fmt = f;
        }
    }
}
