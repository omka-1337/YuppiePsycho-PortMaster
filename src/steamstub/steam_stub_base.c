/*
 * Stand-in libsteam_api for aarch64 handhelds.
 *
 * Valve ships libsteam_api only for x86/x86_64, so a game's Steamworks
 * wrapper (Steamworks.NET, Facepunch.Steamworks) cannot load the real one on
 * an ARM device. This library exports the same flat C API and behaves like a
 * machine where Steam is not running.
 *
 * Environment:
 *   YP_STEAM_STUB=fake        pretend Steam is running offline: SteamAPI_Init
 *                             succeeds and interface accessors return a dummy
 *                             object; every call on it returns 0/false/NULL.
 *                             For games that refuse to start without Steam.
 *   YP_STEAM_LANGUAGE=<lang>  language reported in fake mode (default english)
 *   YP_STEAM_STUB_TRACE=1     log the first call of each entry point to stderr
 *
 * Every other exported symbol is generated from steam_api_symbols.txt by
 * gen_steam_stub.py and simply returns 0.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define YP_EXPORT __attribute__((visibility("default")))

static int yp_mode = -1; /* 0 = Steam not running, 1 = fake offline Steam */
static int yp_trace = -1;
static void *yp_dummy[512] __attribute__((aligned(16)));

static int yp_fake(void)
{
    if (yp_mode < 0) {
        const char *m = getenv("YP_STEAM_STUB");
        yp_mode = (m && strcmp(m, "fake") == 0) ? 1 : 0;
    }
    return yp_mode;
}

static void yp_log(const char *name)
{
    if (yp_trace < 0) {
        const char *t = getenv("YP_STEAM_STUB_TRACE");
        yp_trace = (t && *t && strcmp(t, "0") != 0) ? 1 : 0;
    }
    if (yp_trace)
        fprintf(stderr, "[steam-stub] %s\n", name);
}

#define YP_TRACE(n) do { static int seen_; if (!seen_) { seen_ = 1; yp_log(n); } } while (0)

static void *yp_iface(void)
{
    return yp_fake() ? (void *)yp_dummy : NULL;
}

static const char *yp_language(void)
{
    const char *l = getenv("YP_STEAM_LANGUAGE");
    return (l && *l) ? l : "english";
}

static void yp_errmsg(char *out)
{
    /* SteamErrMsg is char[1024] */
    if (out)
        snprintf(out, 1024, "%s", "Steam is not available on this device (PortMaster stub)");
}

/* ---- initialisation ----------------------------------------------------- */

YP_EXPORT int SteamAPI_Init(void) { YP_TRACE("SteamAPI_Init"); return yp_fake(); }
YP_EXPORT int SteamAPI_InitSafe(void) { YP_TRACE("SteamAPI_InitSafe"); return yp_fake(); }
YP_EXPORT int SteamAPI_InitAnonymousUser(void) { YP_TRACE("SteamAPI_InitAnonymousUser"); return yp_fake(); }

/* ESteamAPIInitResult: 0 = OK, 2 = NoSteamClient */
YP_EXPORT int SteamAPI_InitFlat(char *err)
{
    YP_TRACE("SteamAPI_InitFlat");
    if (yp_fake())
        return 0;
    yp_errmsg(err);
    return 2;
}

YP_EXPORT int SteamInternal_SteamAPI_Init(const char *versions, char *err)
{
    (void)versions;
    YP_TRACE("SteamInternal_SteamAPI_Init");
    if (yp_fake())
        return 0;
    yp_errmsg(err);
    return 2;
}

YP_EXPORT int SteamAPI_RestartAppIfNecessary(uint32_t app_id)
{
    (void)app_id;
    YP_TRACE("SteamAPI_RestartAppIfNecessary");
    return 0;
}

YP_EXPORT int SteamAPI_IsSteamRunning(void) { YP_TRACE("SteamAPI_IsSteamRunning"); return yp_fake(); }
YP_EXPORT int SteamAPI_GetHSteamPipe(void) { YP_TRACE("SteamAPI_GetHSteamPipe"); return yp_fake(); }
YP_EXPORT int SteamAPI_GetHSteamUser(void) { YP_TRACE("SteamAPI_GetHSteamUser"); return yp_fake(); }
YP_EXPORT const char *SteamAPI_GetSteamInstallPath(void) { YP_TRACE("SteamAPI_GetSteamInstallPath"); return ""; }
YP_EXPORT void SteamAPI_Shutdown(void) { YP_TRACE("SteamAPI_Shutdown"); }
YP_EXPORT void SteamAPI_RunCallbacks(void) { YP_TRACE("SteamAPI_RunCallbacks"); }

/* ---- interface lookup --------------------------------------------------- */

YP_EXPORT void *SteamClient(void) { YP_TRACE("SteamClient"); return yp_iface(); }
YP_EXPORT void *SteamGameServerClient(void) { YP_TRACE("SteamGameServerClient"); return NULL; }

YP_EXPORT void *SteamInternal_CreateInterface(const char *version)
{
    (void)version;
    YP_TRACE("SteamInternal_CreateInterface");
    return yp_iface();
}

YP_EXPORT void *SteamInternal_FindOrCreateUserInterface(int user, const char *version)
{
    (void)user; (void)version;
    YP_TRACE("SteamInternal_FindOrCreateUserInterface");
    return yp_iface();
}

YP_EXPORT void *SteamInternal_FindOrCreateGameServerInterface(int user, const char *version)
{
    (void)user; (void)version;
    YP_TRACE("SteamInternal_FindOrCreateGameServerInterface");
    return NULL;
}

YP_EXPORT void *SteamInternal_ContextInit(void *ctx)
{
    YP_TRACE("SteamInternal_ContextInit");
    return yp_fake() ? ctx : NULL;
}

/* ---- strings callers tend to use without a NULL check ------------------- */

YP_EXPORT const char *SteamAPI_ISteamApps_GetCurrentGameLanguage(void *self)
{
    (void)self;
    YP_TRACE("SteamAPI_ISteamApps_GetCurrentGameLanguage");
    return yp_language();
}

YP_EXPORT const char *SteamAPI_ISteamApps_GetAvailableGameLanguages(void *self)
{
    (void)self;
    YP_TRACE("SteamAPI_ISteamApps_GetAvailableGameLanguages");
    return yp_language();
}

YP_EXPORT const char *SteamAPI_ISteamUtils_GetSteamUILanguage(void *self)
{
    (void)self;
    YP_TRACE("SteamAPI_ISteamUtils_GetSteamUILanguage");
    return yp_language();
}

YP_EXPORT const char *SteamAPI_ISteamFriends_GetPersonaName(void *self)
{
    (void)self;
    YP_TRACE("SteamAPI_ISteamFriends_GetPersonaName");
    return "Player";
}

YP_EXPORT const char *SteamAPI_ISteamFriends_GetFriendPersonaName(void *self, uint64_t id)
{
    (void)self; (void)id;
    YP_TRACE("SteamAPI_ISteamFriends_GetFriendPersonaName");
    return "";
}
