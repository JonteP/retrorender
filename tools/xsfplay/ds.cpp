// xsfplay backend: DS: DeSmuME (in_xsf)

#include "xsfplay.h"

#include "desmume/NDSSystem.h"
#include "desmume/MMU.h"
#include "desmume/SPU.h"

volatile bool execute = false;

static struct {
    std::vector<uint8_t> buf;
    unsigned filled = 0, used = 0;
    uint32_t bufferbytes = 0;
} snd;

static int snd_init(int buffersize) {
    snd.bufferbytes = buffersize * sizeof(int16_t);
    snd.buf.assign(snd.bufferbytes + 3, 0);
    snd.filled = snd.used = 0;
    return 0;
}
static void snd_deinit() {}
static void snd_nop() {}
static void snd_volume(int) {}
static uint32_t snd_space() { return snd.bufferbytes >> 2; }
static void snd_update(int16_t *buffer, uint32_t samples) {
    uint32_t bytes = samples << 2;
    if (bytes > snd.bufferbytes) bytes = snd.bufferbytes;
    memcpy(snd.buf.data(), buffer, bytes);
    snd.filled = bytes;
    snd.used = 0;
}

static SoundInterface_struct SNDIF_2SF = {
    1, "2sf Sound Interface", snd_init, snd_deinit, snd_update, snd_space,
    snd_nop, snd_nop, snd_volume, nullptr, nullptr, nullptr};
SoundInterface_struct *SNDCoreList[] = {&SNDIF_2SF, &SNDDummy, nullptr};

static int ds_cb(void *ctx, const uint8_t *exe, size_t exe_size, const uint8_t *, size_t) {
    if (exe_size < 8) return -1;
    std::vector<uint8_t> &rom = *(std::vector<uint8_t> *)ctx;
    uint32_t ofs = le32(exe), size = le32(exe + 4);
    if (size > exe_size - 8) size = (uint32_t)(exe_size - 8);
    // same sizing as in_xsf: a power of two plus a little slack
    uint32_t final_size = pow2(ofs + size);
    if (rom.empty()) rom.resize(final_size + 10, 0);
    else if (rom.size() < (size_t)ofs + size) rom.resize((size_t)ofs + final_size + 10, 0);
    memcpy(&rom[ofs], exe + 8, size);
    return 0;
}

struct DS : Backend {
    std::vector<uint8_t> rom;
    DS(const char *path, const Options &o, const Tags &t) {
        load(path, 0x24, ds_cb, &rom);
        if (NDS_Init()) die("the DS core did not start");
        // The DS mixer runs at 33513982 / 1024 Hz and holds each sample until the next one
        // (no interpolation); DeSmuME's default rate is that rate.
        SetDesmumeSampleRate(32728.498);
        SPU_ChangeSoundCore(1, (int)(32728.498 / 59.837));
        execute = false;
        MMU_unsetRom();
        if (!rom.empty()) {
            NDS_SetROM(rom.data(), (uint32_t)rom.size() - 1);
            gameInfo.loadData((char *)rom.data(), (int)rom.size() - 1);
        }
        // the interpreter is the reference; the JIT (-j) is several times faster
        CommonSettings.use_jit = o.jit;
        CommonSettings.jit_max_block_size = 100;
        CommonSettings.spuInterpolationMode = SPUInterpolation_None;
        NDS_Reset();
        execute = true;
        for (int i = 0; i < t.frames_2sf; ++i) NDS_exec<false>();
        CommonSettings.rigorous_timing = true;
        CommonSettings.spu_advanced = true;
        CommonSettings.advanced_timing = true;
    }
    ~DS() { MMU_unsetRom(); NDS_DeInit(); }
    int rate() override { return 32728; }
    size_t render(int16_t *buf, size_t frames) override {
        unsigned bytes = (unsigned)frames * 4, out = 0;
        uint8_t *dst = (uint8_t *)buf;
        while (bytes) {
            unsigned remain = snd.filled - snd.used;
            if (remain) {
                unsigned n = remain < bytes ? remain : bytes;
                memcpy(dst + out, &snd.buf[snd.used], n);
                snd.used += n; out += n; bytes -= n;
                continue;
            }
            NDS_exec<false>();
            SPU_Emulate_user();
        }
        return frames;
    }
};

Backend *make_ds(const char *path, const Options &o, const Tags &t) { return new DS(path, o, t); }
