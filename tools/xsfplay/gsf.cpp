// xsfplay backend: GBA: mGBA

#include "xsfplay.h"
#include <cstdarg>

extern "C" {
#include <mgba/core/core.h>
#include <mgba/core/blip_buf.h>
#include <mgba/core/log.h>
#include <mgba-util/vfs.h>
}

struct GsfImage { bool entry_set = false; uint32_t entry = 0; std::vector<uint8_t> data; };

static int gsf_cb(void *ctx, const uint8_t *exe, size_t exe_size, const uint8_t *, size_t) {
    if (exe_size < 12) return -1;
    GsfImage *s = (GsfImage *)ctx;
    uint32_t entry = le32(exe), ofs = le32(exe + 4) & 0x1FFFFFF, size = le32(exe + 8);
    if (size > exe_size - 12) size = (uint32_t)(exe_size - 12);
    if (!s->entry_set) { s->entry = entry; s->entry_set = true; }
    uint32_t need = pow2(ofs + size);
    if (s->data.size() < need) s->data.resize(need, 0);
    memcpy(&s->data[ofs], exe + 12, size);
    return 0;
}

static void quiet_log(struct mLogger *, int, enum mLogLevel, const char *, va_list) {}

struct GSF : Backend {
    GsfImage img;
    struct mCore *core = nullptr;
    struct mAVStream stream;
    struct mLogger logger;
    std::vector<int16_t> pending;   // samples made by the last frame, interleaved
    int out_rate;
    static void post(struct mAVStream *s, blip_t *l, blip_t *r) {
        GSF *self = (GSF *)((char *)s - offsetof(GSF, stream));
        int avail = blip_samples_avail(l);
        if (avail <= 0) return;
        size_t at = self->pending.size();
        self->pending.resize(at + (size_t)avail * 2);
        blip_read_samples(l, &self->pending[at], avail, true);
        blip_read_samples(r, &self->pending[at + 1], avail, true);
    }
    GSF(const char *path, const Options &o) : out_rate(o.rate) {
        memset(&logger, 0, sizeof logger);
        logger.log = quiet_log;
        mLogSetDefaultLogger(&logger);
        load(path, 0x22, gsf_cb, &img);
        if (img.data.empty()) die("the GBA program is empty");
        struct VFile *rom = VFileFromConstMemory(img.data.data(), img.data.size());
        core = mCoreFindVF(rom);
        if (!core) die("mGBA does not recognise the GBA program");
        memset(&stream, 0, sizeof stream);
        stream.postAudioBuffer = post;
        core->init(core);
        core->setAVStream(core, &stream);
        mCoreInitConfig(core, nullptr);
        core->setAudioBufferSize(core, 1024);
        blip_set_rates(core->getAudioChannel(core, 0), core->frequency(core), out_rate);
        blip_set_rates(core->getAudioChannel(core, 1), core->frequency(core), out_rate);
        struct mCoreOptions opts = {};
        opts.useBios = false;
        opts.skipBios = true;
        opts.volume = 0x100;
        opts.sampleRate = out_rate;
        mCoreConfigLoadDefaults(&core->config, &opts);
        core->loadROM(core, rom);
        core->reset(core);
    }
    ~GSF() { if (core) core->deinit(core); }
    int rate() override { return out_rate; }
    size_t render(int16_t *buf, size_t frames) override {
        size_t done = 0;
        while (done < frames) {
            if (pending.empty()) {
                for (int i = 0; i < 600 && pending.empty(); ++i) core->runFrame(core);
                if (pending.empty()) {        // 10 s without a full buffer: flush what is there
                    post(&stream, core->getAudioChannel(core, 0), core->getAudioChannel(core, 1));
                    if (pending.empty()) break;
                }
            }
            size_t n = pending.size() / 2;
            if (n > frames - done) n = frames - done;
            memcpy(buf + done * 2, pending.data(), n * 4);
            pending.erase(pending.begin(), pending.begin() + (long)n * 2);
            done += n;
        }
        return done;
    }
};

Backend *make_gsf(const char *path, const Options &o) { return new GSF(path, o); }
