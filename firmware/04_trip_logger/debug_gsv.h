// Core-1-only GSV counters feed the antenna check in all builds and the
// optional debug UI. Does not change fix or GPX construction.
#pragma once
#include <stdint.h>
#include <string.h>

struct DebugSignal {
    int heard = -1, strong = -1, maxCno = -1;
};

class DebugGsv {
    // Three-digit NMEA satellite IDs, sixteen hexadecimal signal IDs.
    // Bitmaps deduplicate satellites across signals without heap allocation.
    static constexpr unsigned WORDS = 32;
    struct Group {
        uint32_t heard[WORDS] = {}, strong[WORDS] = {};
        uint32_t seen = 0;
        uint8_t maxCno = 0;
        bool valid = false;
    } groups[5][16];
    struct Pending {
        Group data;
        uint32_t started = 0;
        unsigned next = 0, messages = 0, satellites = 0, signal = 0;
    } pending[5];
    char sentence[160] = {};
    unsigned length = 0;
    bool collecting = false, ready = false;

    static bool number(const char* text, unsigned& value, unsigned max) {
        if (!*text) return false;
        value = 0;
        for (; *text; ++text) {
            if (*text < '0' || *text > '9') return false;
            value = value * 10 + (*text - '0');
            if (value > max) return false;
        }
        return true;
    }
public:
    // Capture only the sentence body. TinyGPSPlus remains the checksum
    // authority: update() is called only when gps.encode() returns true.
    void encode(char c) {
        if (c == '$') { length = 0; collecting = true; ready = false; }
        else if (collecting && c == '*') {
            sentence[length] = 0;
            collecting = false; ready = true;
        } else if (collecting) {
            if (length + 1 < sizeof sentence) sentence[length++] = c;
            else { collecting = false; ready = false; }
        }
    }

    void update(uint32_t now) {
        if (!ready) return;
        ready = false;
        char* terms[21] = {sentence};
        unsigned n = 1;
        for (char* p = sentence; *p; ++p) {
            if (*p != ',') continue;
            if (n == 21) return;
            *p = 0; terms[n++] = p + 1;
        }
        // GB and legacy BD share one constellation; GQ is NMEA 4.11 QZSS.
        int talker = !strcmp(terms[0], "GPGSV") ? 0 : !strcmp(terms[0], "GLGSV") ? 1 :
                     !strcmp(terms[0], "GAGSV") ? 2 :
                     (!strcmp(terms[0], "GBGSV") || !strcmp(terms[0], "BDGSV")) ? 3 :
                     !strcmp(terms[0], "GQGSV") ? 4 : -1;
        if (talker < 0 || n < 4) return;
        unsigned messages, message, satellites;
        if (!number(terms[1], messages, 25) || !messages ||
            !number(terms[2], message, messages) || !message ||
            !number(terms[3], satellites, 99) ||
            messages != (satellites ? (satellites + 3) / 4 : 1)) return;
        unsigned blocks = message == messages ? satellites - 4 * (message - 1) : 4;
        unsigned end = 4 + 4 * blocks, signal = 0;
        if (n != end && n != end + 1) return;
        if (n == end + 1 && *terms[end]) {
            char c = terms[end][0];
            if (terms[end][1]) return;
            if (c >= '0' && c <= '9') signal = c - '0';
            else if (c >= 'A' && c <= 'F') signal = c - 'A' + 10;
            else return;
        }
        Pending& p = pending[talker];
        if (message == 1) {
            p = Pending(); p.started = now; p.next = 1;
            p.messages = messages; p.satellites = satellites; p.signal = signal;
        }
        if (p.next != message || p.messages != messages || p.satellites != satellites ||
            p.signal != signal || uint32_t(now - p.started) > 15000) {
            p.next = 0; return;
        }
        for (unsigned i = 0; i < blocks; ++i) {
            const char* cnoText = terms[7 + 4 * i];
            if (!*cnoText) continue;
            unsigned cno, svid;
            if (!number(cnoText, cno, 99) ||
                !number(terms[4 + 4 * i], svid, 999) || !svid) {
                p.next = 0; return;
            }
            if (!cno) continue;
            uint32_t bit = uint32_t(1) << (svid % 32);
            p.data.heard[svid / 32] |= bit;
            if (cno >= 30) p.data.strong[svid / 32] |= bit;
            if (cno > p.data.maxCno) p.data.maxCno = cno;
        }
        ++p.next;
        if (message == messages) {
            p.data.valid = true; p.data.seen = now;
            groups[talker][signal] = p.data;
            p.next = 0;
        }
    }

    DebugSignal signal(uint32_t now) const {
        DebugSignal result;
        for (unsigned t = 0; t < 5; ++t) {
            uint32_t heard[WORDS] = {}, strong[WORDS] = {};
            for (const auto& group : groups[t]) {
                if (!group.valid || uint32_t(now - group.seen) > 15000) continue;
                if (result.heard < 0) result.heard = result.strong = result.maxCno = 0;
                if (group.maxCno > result.maxCno) result.maxCno = group.maxCno;
                for (unsigned i = 0; i < WORDS; ++i) {
                    heard[i] |= group.heard[i]; strong[i] |= group.strong[i];
                }
            }
            for (unsigned i = 0; i < WORDS; ++i) {
                result.heard += __builtin_popcount(heard[i]);
                result.strong += __builtin_popcount(strong[i]);
            }
        }
        return result;
    }
};
