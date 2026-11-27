// xsfplay backend: Saturn / Dreamcast: Highly Theoretical

#include "xsfplay.h"

extern "C" {
#include "Core/sega.h"
#include "Core/dcsound.h"
#include "Core/satsound.h"
#include "Core/yam.h"
}

// Merges each program in the chain into one image: 4-byte load address + data.
static int sega_cb(void *ctx, const uint8_t *exe, size_t exe_size, const uint8_t *, size_t) {
    if (exe_size < 4) return -1;
    std::vector<uint8_t> &dst = *(std::vector<uint8_t> *)ctx;
    if (dst.size() < 4) {
        dst.assign(exe, exe + exe_size);
        return 0;
    }
    uint32_t dst_start = le32(&dst[0]) & 0x7FFFFF, src_start = le32(exe) & 0x7FFFFF;
    size_t dst_len = dst.size() - 4, src_len = exe_size - 4;
    if (dst_len > 0x800000) dst_len = 0x800000;
    if (src_len > 0x800000) src_len = 0x800000;
    if (src_start < dst_start) {
        size_t diff = dst_start - src_start;
        dst.resize(dst_len + 4 + diff);
        memmove(&dst[4 + diff], &dst[4], dst_len);
        memset(&dst[4], 0, diff);
        dst_len += diff;
        dst_start = src_start;
        dst[0] = dst_start & 0xFF; dst[1] = (dst_start >> 8) & 0xFF;
        dst[2] = (dst_start >> 16) & 0xFF; dst[3] = (dst_start >> 24) & 0xFF;
    }
    if (src_start + src_len > dst_start + dst_len) {
        size_t diff = (src_start + src_len) - (dst_start + dst_len);
        dst.resize(dst_len + 4 + diff);
        memset(&dst[4 + dst_len], 0, diff);
        dst_len += diff;
    }
    memcpy(&dst[4 + (src_start - dst_start)], exe + 4, src_len);
    return 0;
}

struct HT : Backend {
    std::vector<uint8_t> state, program;
    int version;
    HT(const char *path, int ver) : version(ver) {
        if (sega_init()) die("the Saturn/Dreamcast core did not start");
        load(path, ver, sega_cb, &program);
        uint8_t v = ver == 0x11 ? 1 : 2;
        state.resize(sega_get_state_size(v));
        sega_clear_state(state.data(), v);
        sega_enable_dry(state.data(), 1);
        sega_enable_dsp(state.data(), 1);
        sega_enable_dsp_dynarec(state.data(), 0);  // the interpreter is the reference
        if (sega_upload_program(state.data(), program.data(), (uint32_t)program.size()))
            die("the Saturn/Dreamcast program did not load");
    }
    int rate() override { return 44100; }
    size_t render(int16_t *buf, size_t frames) override {
        size_t done = 0;
        while (done < frames) {
            uint32_t n = (uint32_t)(frames - done);
            sint32 r = sega_execute(state.data(), 0x7FFFFFFF, buf + done * 2, &n);
            if (r < 0) die("the Saturn/Dreamcast program crashed");
            done += n;
            if (!n) break;
        }
        return done;
    }
};

Backend *make_ht(const char *path, int version) { return new HT(path, version); }
