#pragma once
#if defined(JOURNAL_HOST_TEST)
#include <journal_fake_io.h>
#else
#include "journal_vfs.h"
using JournalPlatformIo = diagnostics::JournalVfs;
#endif
