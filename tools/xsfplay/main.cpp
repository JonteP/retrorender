// xsfplay - render xSF files (PSF family) to WAV on stdout.
//
// One small command line program around the most accurate open emulator cores we know of:
//
//   psf, psf2   Highly Experimental (Neill Corlett)   PS1 / PS2 IOP + SPU, needs a BIOS image
//   ssf, dsf    Highly Theoretical (Neill Corlett)    Saturn SCSP / Dreamcast AICA
//   usf         lazyusf2 (mupen64plus based)          N64, low level RSP emulation
//   gsf         mGBA                                  GBA
//   2sf         DeSmuME from in_xsf (CyberBotX)       DS, no interpolation at the hardware rate
//
// All of them get their program data through psflib, which follows the _lib/_libN chains.
// Output is s16le stereo WAV with an open-ended length; the caller cuts and fades.
//
// Usage: xsfplay [-r rate] [-n frames] [-b bios] [-t] [-c harsh|friendly] [-j] file
//   -r  output rate for the cores that can produce any rate (usf, gsf); default 44100
//   -n  stop after this many frames (default: run until stdout closes)
//   -b  HE BIOS image (hebios.bin, or a PS2 BIOS that is converted on the fly)
//   -t  print timing and the native rate to stderr when done (for benchmarks)
//   -c  PS1/PS2 DMA timing: harsh (default, like the hardware) or friendly
//   -j  DS: use DeSmuME's JIT instead of the interpreter (faster, slightly less exact)
//   -I  print the PSF version byte and exit
//
// The program is GPL (Highly Theoretical is GPLv3); it runs as a separate process.

#include <cstdint>
#include <cstdio>

#include <ctime>
#include <strings.h>
#include <unistd.h>

#include "xsfplay.h"

[[noreturn]] void die(const char *fmt, const char *arg) {
    fprintf(stderr, "xsfplay: ");
    fprintf(stderr, fmt, arg);
    fputc('\n', stderr);
    exit(1);
}

// ---------------------------------------------------------------- psflib file access

static void *f_open(void *, const char *path) { return fopen(path, "rb"); }
static size_t f_read(void *p, size_t s, size_t c, void *f) { return fread(p, s, c, (FILE *)f); }
static int f_seek(void *f, int64_t o, int w) { return fseek((FILE *)f, (long)o, w); }
static int f_close(void *f) { return fclose((FILE *)f); }
static long f_tell(void *f) { return ftell((FILE *)f); }
static const psf_file_callbacks FS = {"\\/", nullptr, f_open, f_read, f_seek, f_close, f_tell};

static std::string g_messages;
static void on_status(void *, const char *m) { g_messages += m; }

int load(const char *path, uint8_t version, psf_load_callback cb, void *ctx,
         psf_info_callback info, void *info_ctx) {
    g_messages.clear();
    int r = psf_load(path, &FS, version, cb, ctx, info, info_ctx, 0, on_status, nullptr);
    if (r < 0) {
        // psflib logs every step; keep the line that says what went wrong
        std::string why = "could not load the file or one of its _lib files";
        size_t at = 0;
        while (at < g_messages.size()) {
            size_t end = g_messages.find('\n', at);
            if (end == std::string::npos) end = g_messages.size();
            std::string line = g_messages.substr(at, end - at);
            for (const char *k : {"rror", "ailed", "nvalid", "nable", "ould not", "not found", "issing"})
                if (line.find(k) != std::string::npos) { why = line; break; }
            at = end + 1;
        }
        while (!why.empty() && (why[0] == ' ' || why[0] == '\t')) why.erase(0, 1);
        if (why.find("Data handler") != std::string::npos)
            why = "the emulator rejected the program data";
        die("%s", why.c_str());
    }
    return r;
}

static int tag_cb(void *ctx, const char *name, const char *value) {
    Tags *t = (Tags *)ctx;
    if (!strcasecmp(name, "_enablecompare") && *value) t->usf_compare = true;
    else if (!strcasecmp(name, "_enablefifofull") && *value) t->usf_fifo = true;
    else if (!strcasecmp(name, "_refresh")) t->refresh = atoi(value);
    else if (!strcasecmp(name, "_frames")) t->frames_2sf = atoi(value);
    return 0;
}

// ---------------------------------------------------------------- main

static void put32(uint8_t *p, uint32_t v) { p[0] = v; p[1] = v >> 8; p[2] = v >> 16; p[3] = v >> 24; }
static void put16(uint8_t *p, uint16_t v) { p[0] = v; p[1] = v >> 8; }

static void wav_header(int rate) {
    uint8_t h[44];
    memcpy(h, "RIFF", 4); put32(h + 4, 0xFFFFFFFF); memcpy(h + 8, "WAVEfmt ", 8);
    put32(h + 16, 16); put16(h + 20, 1); put16(h + 22, 2); put32(h + 24, rate);
    put32(h + 28, rate * 4); put16(h + 32, 4); put16(h + 34, 16);
    memcpy(h + 36, "data", 4); put32(h + 40, 0xFFFFFFFF);
    fwrite(h, 1, 44, stdout);
}

int main(int argc, char **argv) {
    Options o;
    long long limit = -1;
    bool timing = false, ident = false;
    int c;
    while ((c = getopt(argc, argv, "r:n:b:tc:Ij")) != -1) {
        switch (c) {
        case 'r': o.rate = atoi(optarg); break;
        case 'n': limit = atoll(optarg); break;
        case 'b': o.bios = optarg; break;
        case 't': timing = true; break;
        case 'c': o.harsh = strcmp(optarg, "friendly") != 0; break;
        case 'I': ident = true; break;
        case 'j': o.jit = true; break;
        default: return 2;
        }
    }
    if (optind >= argc) die("usage: xsfplay [-r rate] [-n frames] [-b bios] [-t] [-c harsh|friendly] [-j] file");
    const char *path = argv[optind];
    if (o.rate < 8000 || o.rate > 192000) die("bad rate");

    Tags tags;
    int version = load(path, 0, nullptr, nullptr, tag_cb, &tags);
    if (ident) { printf("0x%02x\n", version); return 0; }

    Backend *b = nullptr;
    switch (version) {
#ifdef WITH_HE
    case 0x01: case 0x02: b = make_he(path, version, o, tags); break;
#endif
#ifdef WITH_HT
    case 0x11: case 0x12: b = make_ht(path, version); break;
#endif
#ifdef WITH_USF
    case 0x21: b = make_usf(path, o, tags); break;
#endif
#ifdef WITH_GSF
    case 0x22: b = make_gsf(path, o); break;
#endif
#ifdef WITH_2SF
    case 0x24: b = make_ds(path, o, tags); break;
#endif
    default: {
        char v[8];
        snprintf(v, sizeof v, "0x%02x", version);
        die("this build has no core for PSF version %s", v);
    }
    }

    wav_header(b->rate());
    std::vector<int16_t> buf(4096 * 2);
    long long total = 0;
    clock_t t0 = clock();
    while (limit < 0 || total < limit) {
        size_t want = buf.size() / 2;
        if (limit >= 0 && (long long)want > limit - total) want = (size_t)(limit - total);
        size_t got = b->render(buf.data(), want);
        if (!got) break;
        if (fwrite(buf.data(), 4, got, stdout) != got) break;
        total += (long long)got;
    }
    fflush(stdout);
    if (timing) {
        double s = (double)(clock() - t0) / CLOCKS_PER_SEC;
        fprintf(stderr, "rate=%d frames=%lld cpu=%.3fs speed=%.1fx\n", b->rate(), total, s,
                s > 0 ? (double)total / b->rate() / s : 0.0);
    }
    delete b;
    return 0;
}
