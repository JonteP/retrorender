// Stand-in for in_xsf's XSFCommon.h, which pulls in Windows string conversion.
// SPU.cpp only needs the standard headers it brought along.
#pragma once
#define _USE_MATH_DEFINES
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <type_traits>
