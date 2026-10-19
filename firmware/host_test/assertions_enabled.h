// Force-included by every host test build: silent assertion removal is a failure.
#pragma once
#include <cassert>

#ifdef NDEBUG
#error "Host tests require active assert(); compile with -UNDEBUG"
#endif
