// Shared pieces of xsfplay: the backend interface, options and small helpers.
#pragma once

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

extern "C" {
#include "psflib.h"
}

[[noreturn]] void die(const char *fmt, const char *arg = "");

inline uint32_t pow2(uint32_t v) {
    v -= 1; v |= v >> 1; v |= v >> 2; v |= v >> 4; v |= v >> 8; v |= v >> 16; return v + 1;
}

inline uint32_t le32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

// Runs psflib over the whole _lib chain; dies with psflib's message on failure.
int load(const char *path, uint8_t version, psf_load_callback cb, void *ctx,
         psf_info_callback info = nullptr, void *info_ctx = nullptr);

struct Backend {
    virtual ~Backend() {}
    virtual int rate() = 0;
    // fill `frames` stereo frames; returns how many were made (0 = the program ended)
    virtual size_t render(int16_t *buf, size_t frames) = 0;
};

struct Options {
    int rate = 44100;
    const char *bios = nullptr;
    bool harsh = true;
    bool jit = false;
};

// Tags that change how a file must be played. Values are taken from the outermost file.
struct Tags {
    bool usf_compare = false, usf_fifo = false;
    int refresh = 0;
    int frames_2sf = -1;          // DS: frames to run before the music starts
};

Backend *make_he(const char *path, int version, const Options &o, const Tags &t);
Backend *make_ht(const char *path, int version);
Backend *make_usf(const char *path, const Options &o, const Tags &t);
Backend *make_gsf(const char *path, const Options &o);
Backend *make_ds(const char *path, const Options &o, const Tags &t);
