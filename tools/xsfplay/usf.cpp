// xsfplay backend: N64: lazyusf2

#include "xsfplay.h"

extern "C" {
#include "usf/usf.h"
}

static int usf_cb(void *ctx, const uint8_t *exe, size_t exe_size, const uint8_t *res, size_t res_size) {
    if (exe && exe_size > 0) return -1;
    return usf_upload_section(ctx, res, res_size);
}

struct USF : Backend {
    std::vector<uint8_t> state;
    int out_rate;
    bool started = false;
    USF(const char *path, const Options &o, const Tags &t) : out_rate(o.rate) {
        state.resize(usf_get_state_size());
        usf_clear(state.data());
        load(path, 0x21, usf_cb, state.data());
        usf_set_compare(state.data(), t.usf_compare);
        usf_set_fifo_full(state.data(), t.usf_fifo);
        usf_set_hle_audio(state.data(), 0);     // low level RSP: slower, but the real microcode runs
    }
    ~USF() { if (started) usf_shutdown(state.data()); }
    int rate() override { return out_rate; }
    size_t render(int16_t *buf, size_t frames) override {
        // the N64 picks its own DAC rate and may change it; lazyusf2's sinc resampler
        // turns that into one steady output rate
        const char *err = usf_render_resampled(state.data(), buf, frames, out_rate);
        started = true;
        if (err) die("%s", err);
        return frames;
    }
};

Backend *make_usf(const char *path, const Options &o, const Tags &t) { return new USF(path, o, t); }
