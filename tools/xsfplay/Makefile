# Builds xsfplay from checked-out emulator sources. The Dockerfile clones them at pinned
# commits; for a local build point the variables at your own checkouts:
#
#   make PSFLIB=../psflib HE=../Highly_Experimental HT=../Highly_Theoretical \
#        USF=../lazyusf2 MGBA=../mgba MGBA_BUILD=../mgba/build IN_XSF=../in_xsf
#
# Leave a variable empty to build without that core.

PSFLIB     ?=
HE         ?=
HT         ?=
USF        ?=
MGBA       ?=
MGBA_BUILD ?= $(MGBA)/build
IN_XSF     ?=

CC       ?= cc
CXX      ?= c++
OPT      ?= -O2
OUT      ?= build
CFLAGS   += $(OPT) -fPIC -w
CXXFLAGS += $(OPT) -fPIC -w -std=c++20
LIBS      = -lz -lm

all: $(OUT)/xsfplay

ifeq ($(PSFLIB),)
$(error PSFLIB must point at a psflib checkout)
endif

OBJS     = $(OUT)/main.o $(OUT)/psflib.o $(OUT)/psf2fs.o
DEFS     =
EXTRA    =

# Highly Experimental: PS1/PS2
ifneq ($(HE),)
DEFS += -DWITH_HE
OBJS += $(OUT)/he.o
$(OUT)/he.o: he.cpp xsfplay.h
	@mkdir -p $(OUT)
	$(CXX) $(CXXFLAGS) -I$(PSFLIB) -I$(HE) -c $< -o $@
HE_SRC = psx.c ioptimer.c iop.c bios.c r3000dis.c r3000asm.c r3000.c vfs.c spucore.c spu.c mkhebios.c
HE_OBJ = $(addprefix $(OUT)/he/,$(HE_SRC:.c=.o))
OBJS += $(HE_OBJ)
$(OUT)/he/%.o: $(HE)/Core/%.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -DEMU_COMPILE -DEMU_LITTLE_ENDIAN -DHAVE_STDINT_H -c $< -o $@
endif

# Highly Theoretical: Saturn/Dreamcast (Musashi 68000)
ifneq ($(HT),)
DEFS += -DWITH_HT
OBJS += $(OUT)/ht.o
$(OUT)/ht.o: ht.cpp xsfplay.h
	@mkdir -p $(OUT)
	$(CXX) $(CXXFLAGS) -I$(PSFLIB) -I$(HT) -c $< -o $@
HT_SRC = sega.c dcsound.c satsound.c yam.c arm.c m68k/m68kops.c m68k/m68kcpu.c
HT_OBJ = $(addprefix $(OUT)/ht/,$(HT_SRC:.c=.o))
OBJS += $(HT_OBJ)
$(OUT)/ht/%.o: $(HT)/Core/%.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -DEMU_COMPILE -DEMU_LITTLE_ENDIAN -DHAVE_STDINT_H -DUSE_M68K -DLSB_FIRST -DHAVE_MPROTECT -I$(HT)/Core -c $< -o $@
endif

# lazyusf2: N64 (built with its own Makefile)
ifneq ($(USF),)
DEFS += -DWITH_USF
OBJS += $(OUT)/usf.o
$(OUT)/usf.o: usf.cpp xsfplay.h
	@mkdir -p $(OUT)
	$(CXX) $(CXXFLAGS) -I$(PSFLIB) -I$(USF) -c $< -o $@
EXTRA += $(USF)/liblazyusf.a
$(USF)/liblazyusf.a:
	$(MAKE) -C $(USF) liblazyusf.a OPTFLAGS=-O2
endif

# mGBA: GBA (libmgba.a built by cmake, see the Dockerfile)
ifneq ($(MGBA),)
DEFS += -DWITH_GSF
OBJS += $(OUT)/gsf.o
$(OUT)/gsf.o: gsf.cpp xsfplay.h
	@mkdir -p $(OUT)
	$(CXX) $(CXXFLAGS) -I$(PSFLIB) -I$(MGBA)/include -I$(MGBA_BUILD)/include -c $< -o $@
EXTRA += $(MGBA_BUILD)/libmgba.a
LIBS += -lpthread
endif

# DeSmuME from in_xsf: DS
ifneq ($(IN_XSF),)
DEFS += -DWITH_2SF
DS_DIR = $(IN_XSF)/src/in_2sf
OBJS += $(OUT)/ds.o
$(OUT)/ds.o: ds.cpp xsfplay.h
	@mkdir -p $(OUT)
	$(CXX) $(CXXFLAGS) -I$(PSFLIB) -Ishim -I$(DS_DIR) -c $< -o $@
# the DeSmuME and SPU sources, as in_2sf's own CMakeLists.txt lists them
DS_SRC = $(shell grep -oE '^[[:space:]]*(desmume|spu)/[A-Za-z0-9_/.]+\.cpp' $(DS_DIR)/CMakeLists.txt)
DS_OBJ = $(addprefix $(OUT)/ds/,$(DS_SRC:.cpp=.o))
OBJS += $(DS_OBJ)
# in_xsf is a Winamp plugin: shim/ replaces the two Windows-only headers it pulls in
$(OUT)/ds/%.o: $(DS_DIR)/%.cpp
	@mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS) -Ishim -I$(DS_DIR) -I$(DS_DIR)/desmume -c $< -o $@
endif

$(OUT)/xsfplay: $(OBJS) $(EXTRA)
	$(CXX) -o $@ $(OBJS) $(EXTRA) $(LIBS)

$(OUT)/main.o: main.cpp xsfplay.h
	@mkdir -p $(OUT)
	$(CXX) $(CXXFLAGS) $(DEFS) -I$(PSFLIB) -c $< -o $@

$(OUT)/psflib.o: $(PSFLIB)/psflib.c
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/psf2fs.o: $(PSFLIB)/psf2fs.c
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@

clean:
	rm -rf $(OUT)

.PHONY: all clean
