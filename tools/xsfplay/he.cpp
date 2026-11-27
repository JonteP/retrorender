// xsfplay backend: PS1 / PS2: Highly Experimental

#include "xsfplay.h"
#include <strings.h>

extern "C" {
#include "psf2fs.h"
#include "Core/psx.h"
#include "Core/iop.h"
#include "Core/r3000.h"
#include "Core/bios.h"
#include "Core/mkhebios.h"
}

static std::vector<uint8_t> read_file(const char *path) {
    std::vector<uint8_t> d;
    FILE *f = fopen(path, "rb");
    if (!f) return d;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (n > 0) {
        d.resize((size_t)n);
        if (fread(d.data(), 1, d.size(), f) != d.size()) d.clear();
    }
    fclose(f);
    return d;
}

// PS-X EXE loading as foo_psf does it: psflib hands over the _lib first, then the minipsf.
// Every text section goes into RAM, but PC/SP and the region come from the first one (the
// _lib); a minipsf's own PC is not meant to be used and crashes the CPU.
struct Psf1Load { void *state; bool first = true; int refresh = 0; };

static int psf1_cb(void *ctx, const uint8_t *exe, size_t exe_size, const uint8_t *, size_t) {
    Psf1Load *l = (Psf1Load *)ctx;
    if (exe_size < 0x800 || memcmp(exe, "PS-X EXE", 8)) return -1;
    uint32_t addr = le32(exe + 0x18) & 0x1FFFFF, size = (uint32_t)(exe_size - 0x800);
    if (addr < 0x10000 || size > 0x1F0000 || addr + size > 0x200000) return -1;
    void *iop = psx_get_iop_state(l->state);
    iop_upload_to_ram(iop, addr, (void *)(exe + 0x800), size);
    if (!l->refresh) {
        if (!strncasecmp((const char *)exe + 113, "Japan", 5)) l->refresh = 60;
        else if (!strncasecmp((const char *)exe + 113, "Europe", 6)) l->refresh = 50;
        else if (!strncasecmp((const char *)exe + 113, "North America", 13)) l->refresh = 60;
    }
    if (l->first) {
        void *cpu = iop_get_r3000_state(iop);
        r3000_setreg(cpu, R3000_REG_PC, le32(exe + 0x10));
        r3000_setreg(cpu, R3000_REG_GEN + 29, le32(exe + 0x30));
        l->first = false;
    }
    return 0;
}

struct HE : Backend {
    std::vector<uint8_t> state;
    void *fs = nullptr;
    int version;
    HE(const char *path, int ver, const Options &o, const Tags &t) : version(ver) {
        if (!o.bios) die("PS1/PS2 needs a BIOS image (hebios.bin or a PS2 BIOS)");
        static std::vector<uint8_t> image = read_file(o.bios);
        if (image.empty()) die("cannot read the BIOS image %s", o.bios);
        // A full PS2 BIOS (4 MB) is cut down to the modules HE needs; hebios.bin is used as is.
        if (image.size() >= 0x400000) {
            int size = 0x400000;                 // mkhebios wants exactly one 4 MB image
            void *he = mkhebios_create(image.data(), &size);
            if (!he || size <= 0) die("%s is not a PS2 BIOS that HE can use (it was made for a North American one)", o.bios);
            image.assign((uint8_t *)he, (uint8_t *)he + size);
            mkhebios_delete(he);
        } else if (image.size() < 0x100 || memcmp(&image[0x80], "Highly Experimental", 19) != 0) {
            // a plain PS1 BIOS (SCPH-1001 etc.) has no HE environment block and cannot be used
            die("%s is not hebios.bin or a PS2 BIOS (a PS1 BIOS does not work with HE)", o.bios);
        }
        uint32_t n = (uint32_t)image.size();
        if (n & (n - 1)) die("%s has the wrong size for a BIOS image", o.bios);
        bios_set_image(image.data(), (uint32_t)image.size());
        if (psx_init()) die("the PS1/PS2 core did not start");
        state.resize(psx_get_state_size(ver));
        psx_clear_state(state.data(), ver);
        if (ver == 1) {
            Psf1Load l;
            l.state = state.data();
            load(path, 1, psf1_cb, &l);
            if (t.refresh) psx_set_refresh(state.data(), t.refresh);
            else if (l.refresh) psx_set_refresh(state.data(), l.refresh);
        } else {
            fs = psf2fs_create();
            load(path, 2, psf2fs_load_callback, fs);
            psx_set_readfile(state.data(), (psx_readfile_t)psf2fs_virtual_readfile, fs);
            if (t.refresh) psx_set_refresh(state.data(), t.refresh);
        }
        iop_set_compat(psx_get_iop_state(state.data()), o.harsh ? IOP_COMPAT_HARSH : IOP_COMPAT_FRIENDLY);
    }
    ~HE() { if (fs) psf2fs_delete(fs); }
    int rate() override { return version == 1 ? 44100 : 48000; }
    size_t render(int16_t *buf, size_t frames) override {
        size_t done = 0;
        while (done < frames) {
            uint32_t n = (uint32_t)(frames - done);
            sint32 r = psx_execute(state.data(), 0x7FFFFFFF, buf + done * 2, &n, 0);
            if (r < -1) die("the PS1/PS2 program crashed");
            done += n;
            if (r == -1) break;          // PS2 program halted
        }
        return done;
    }
};

Backend *make_he(const char *path, int version, const Options &o, const Tags &t) {
    return new HE(path, version, o, t);
}
