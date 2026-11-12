/*
 * bench.c - deterministic CPU workloads for PPW measurement.
 *
 * Builds on Windows (Win32 threads, MinGW-w64) and Linux/POSIX (pthreads):
 *   gcc -O2 -static bench.c -o bench.exe            (Windows)
 *   gcc -O2 -static -pthread bench.c -o bench       (Linux)
 *
 * Usage: bench <threads> <seconds> [inner] [profile]
 *        bench --caps                         (profiles this CPU can run)
 *
 * Profiles (workload v2):
 *   scalar  dependent integer chain + FP chains (default; general-purpose code)
 *   avx2    256-bit FP32 FMA throughput (AVX2 + FMA): 1 unit = 192 FLOP
 *   aes     AES-128 encryption of 8 parallel blocks (AES-NI): 1 unit = 128 bytes
 * ISA profiles are compiled with function target attributes and chosen at
 * run time, so one binary runs everywhere; unsupported ones are refused.
 *
 * Emits on stdout:
 *   BUILD <compiler version>
 *   WORKLOAD <version>
 *   PROFILE <name>
 *   UNIT <name> <per_work_unit>                (only for ISA profiles)
 *   THREADS <n>
 *   START
 *   TICK <elapsed_ms> <total_work_units>      (about once per second, actual time)
 *   ...
 *   STOP <elapsed_ms> <total_work_units>      (workload ends)
 *   FREQ <thread> <cpu> <mhz>                 (one per thread, see probe)
 *   DONE <elapsed_ms> <total_work_units>
 *
 * 1 work unit = one fixed inner-loop iteration, identical for every thread
 * and every machine, so the score is "work units per second".
 */
#if !defined(_WIN32) && !defined(_GNU_SOURCE)
#define _GNU_SOURCE /* sched_getcpu; must precede every system header */
#endif
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <stdatomic.h>

#ifdef _WIN32
#include <windows.h>
#include <malloc.h>
#else
#include <pthread.h>
#include <sched.h>
#include <time.h>
#endif

#if defined(__GNUC__) && (defined(__x86_64__) || defined(__i386__))
#define HAVE_X86_ISA 1
#include <cpuid.h>
#include <immintrin.h>
#endif

#define WORKLOAD_VERSION 2

#ifndef DEFAULT_INNER
#define DEFAULT_INNER 4096
#endif

/* One slot per worker on its own 64-byte cache line (the allocation is
 * 64-byte aligned too): a shared line makes cores fight over it, which costs
 * throughput and watts and skews the very thing being measured. */
typedef struct {
    _Alignas(64) _Atomic unsigned long long work;
    uint64_t sink;
    double probe_mhz; /* measured core clock right after the workload */
    int probe_cpu;    /* logical CPU the probe ran on, -1 if unknown */
} slot_t;

enum { PROFILE_SCALAR, PROFILE_AVX2, PROFILE_AES };

static slot_t *g_slots = NULL;
static int g_threads = 1;
static atomic_int g_stop = 0;
static int g_inner = DEFAULT_INNER;
static double g_seconds = 30.0;
static int g_profile = PROFILE_SCALAR;
static volatile uint64_t g_sink = 0;

static void *alloc_aligned(size_t size)
{
    void *p;
#ifdef _WIN32
    p = _aligned_malloc(size, 64);
#else
    if (posix_memalign(&p, 64, size) != 0)
        p = NULL;
#endif
    if (p)
        memset(p, 0, size);
    return p;
}

static void free_aligned(void *p)
{
#ifdef _WIN32
    _aligned_free(p);
#else
    free(p);
#endif
}

/* ---------------------------------------------------------------- timing -- */
#ifdef _WIN32
static LARGE_INTEGER g_t0, g_freq;

static void clock_init(void)
{
    QueryPerformanceFrequency(&g_freq);
    QueryPerformanceCounter(&g_t0);
}

static double elapsed_ms(void)
{
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    return (double)(now.QuadPart - g_t0.QuadPart) * 1000.0 / (double)g_freq.QuadPart;
}

static void sleep_ms(int ms) { Sleep((DWORD)ms); }
#else
static struct timespec g_t0;

static void clock_init(void) { clock_gettime(CLOCK_MONOTONIC, &g_t0); }

static double elapsed_ms(void)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (double)(now.tv_sec - g_t0.tv_sec) * 1000.0 +
           (double)(now.tv_nsec - g_t0.tv_nsec) / 1e6;
}

static void sleep_ms(int ms)
{
    struct timespec ts = {ms / 1000, (long)(ms % 1000) * 1000000L};
    nanosleep(&ts, NULL);
}
#endif

/* ------------------------------------------------------------ clock probe -- */

#ifndef PROBE_MS
#define PROBE_MS 250
#endif

static int current_cpu(void)
{
#ifdef _WIN32
    return (int)GetCurrentProcessorNumber();
#else
    return sched_getcpu();
#endif
}

#if defined(__GNUC__) && (defined(__x86_64__) || defined(__i386__) || defined(__aarch64__))
#define HAVE_PROBE 1
/* 100 dependent register+register adds: exactly 100 core clock cycles on any
 * modern x86/ARM core. Register+register on purpose: recent Intel P-cores
 * can fold add-with-immediate chains at rename, which would read too high.
 * The loop counter runs in parallel and adds nothing to the chain. */
static uint64_t add_chain(uint64_t blocks, uint64_t one)
{
    uint64_t x = 0;
    for (uint64_t i = 0; i < blocks; ++i) {
#if defined(__aarch64__)
        __asm__ volatile(".rept 100\n\tadd %0, %0, %1\n\t.endr" : "+r"(x) : "r"(one));
#else
        __asm__ volatile(".rept 100\n\tadd %1, %0\n\t.endr" : "+r"(x) : "r"(one));
#endif
    }
    return x;
}

/* Effective clock of the core this thread runs on, in MHz. Runs right after
 * the workload, without idling, so the core is still in the same state.
 * Independent of OS counters (which are unreliable on hybrid CPUs). */
static double probe_mhz(uint64_t one)
{
    const uint64_t chunk = 2000; /* 200k cycles per time check */
    uint64_t blocks = 0;
    volatile uint64_t sink = 0;
    sink += add_chain(chunk, one); /* warm-up */
    double t0 = elapsed_ms(), t;
    do {
        sink += add_chain(chunk, one);
        blocks += chunk;
        t = elapsed_ms();
    } while (t - t0 < PROBE_MS);
    (void)sink;
    return (double)blocks * 100.0 / ((t - t0) * 1000.0); /* cycles/us = MHz */
}
#endif

/* ------------------------------------------------------- CPU capabilities -- */

#ifdef HAVE_X86_ISA
static int cpu_has_avx2_fma(void)
{
    unsigned a, b, c, d;
    if (!__get_cpuid(1, &a, &b, &c, &d))
        return 0;
    int fma = (c >> 12) & 1, osxsave = (c >> 27) & 1, avx = (c >> 28) & 1;
    if (!(fma && osxsave && avx))
        return 0;
    /* the OS must save the YMM state (XCR0 bits 1 and 2) */
    unsigned xlo, xhi;
    __asm__ volatile("xgetbv" : "=a"(xlo), "=d"(xhi) : "c"(0));
    (void)xhi;
    if ((xlo & 6u) != 6u)
        return 0;
    if (__get_cpuid_max(0, NULL) < 7)
        return 0;
    __cpuid_count(7, 0, a, b, c, d);
    return (int)((b >> 5) & 1u);
}

static int cpu_has_aes(void)
{
    unsigned a, b, c, d;
    if (!__get_cpuid(1, &a, &b, &c, &d))
        return 0;
    return (int)((c >> 25) & 1u) && (int)((d >> 26) & 1u); /* AES-NI + SSE2 */
}
#else
static int cpu_has_avx2_fma(void) { return 0; }
static int cpu_has_aes(void) { return 0; }
#endif

/* --------------------------------------------------------------- workload -- */

static int should_stop(void) { return atomic_load_explicit(&g_stop, memory_order_relaxed); }

/* Dependent integer chain + FP chains: not trivially vectorizable, keeps the
 * core busy with real ALU/FPU work instead of memory traffic. Workload v2:
 * the FP recurrence converges (multiplier < 1) instead of growing to inf
 * after ~6e9 iterations, as v1 did. */
static void run_scalar(slot_t *slot, intptr_t tid)
{
    uint64_t x = 0x9E3779B97F4A7C15ULL * (uint64_t)(tid + 1) + 1;
    double a = 1.0 + (double)tid * 0.001;
    double b = 2.0;
    double c = 0.5;
    unsigned long long local = 0;

    while (!should_stop()) {
        for (int k = 0; k < g_inner; ++k) {
            x = x * 6364136223846793005ULL + 1442695040888963407ULL;
            uint64_t y = x ^ (x >> 29);
            y *= 0x9E3779B97F4A7C15ULL;
            y ^= y >> 32;
            x += y;

            a = a * 0.9999998807907104 + b * 1.1920928955078125e-7;
            b = b - c * 1e-7;
            c = c + 1e-9;
            if ((y & 0xFFFFu) == 0x1234u)
                a += (double)(y & 0xFFFFFF) * 1e-9;
        }
        local += (unsigned long long)g_inner;
        /* single writer per slot: a relaxed store is enough */
        atomic_store_explicit(&slot->work, local, memory_order_relaxed);
    }
    /* keep the compiler from removing the chains (read after join) */
    slot->sink = (uint64_t)(int64_t)(a + b + c) ^ x ^ local;
}

#ifdef HAVE_X86_ISA
/* 12 independent 256-bit FMA chains: enough to cover FMA latency (4 cycles)
 * on two FMA ports. acc = acc * m + c converges to c / (1 - m), so values stay
 * normal (no denormals, no inf). 1 unit = 12 FMA x 8 lanes x 2 = 192 FLOP. */
#define AVX2_FLOP_PER_UNIT 192
__attribute__((target("avx2,fma"))) static void run_avx2(slot_t *slot, intptr_t tid)
{
    const __m256 m = _mm256_set1_ps(0.9999f);
    const __m256 c = _mm256_set1_ps(1e-4f * (float)(tid + 1));
    __m256 a0 = _mm256_set1_ps(1.00f), a1 = _mm256_set1_ps(1.01f), a2 = _mm256_set1_ps(1.02f);
    __m256 a3 = _mm256_set1_ps(1.03f), a4 = _mm256_set1_ps(1.04f), a5 = _mm256_set1_ps(1.05f);
    __m256 a6 = _mm256_set1_ps(1.06f), a7 = _mm256_set1_ps(1.07f), a8 = _mm256_set1_ps(1.08f);
    __m256 a9 = _mm256_set1_ps(1.09f), a10 = _mm256_set1_ps(1.10f), a11 = _mm256_set1_ps(1.11f);
    unsigned long long local = 0;

    while (!should_stop()) {
        for (int k = 0; k < g_inner; ++k) {
            a0 = _mm256_fmadd_ps(a0, m, c);
            a1 = _mm256_fmadd_ps(a1, m, c);
            a2 = _mm256_fmadd_ps(a2, m, c);
            a3 = _mm256_fmadd_ps(a3, m, c);
            a4 = _mm256_fmadd_ps(a4, m, c);
            a5 = _mm256_fmadd_ps(a5, m, c);
            a6 = _mm256_fmadd_ps(a6, m, c);
            a7 = _mm256_fmadd_ps(a7, m, c);
            a8 = _mm256_fmadd_ps(a8, m, c);
            a9 = _mm256_fmadd_ps(a9, m, c);
            a10 = _mm256_fmadd_ps(a10, m, c);
            a11 = _mm256_fmadd_ps(a11, m, c);
        }
        local += (unsigned long long)g_inner;
        atomic_store_explicit(&slot->work, local, memory_order_relaxed);
    }
    __m256 s = _mm256_add_ps(_mm256_add_ps(_mm256_add_ps(a0, a1), _mm256_add_ps(a2, a3)),
                             _mm256_add_ps(_mm256_add_ps(a4, a5), _mm256_add_ps(a6, a7)));
    s = _mm256_add_ps(s, _mm256_add_ps(_mm256_add_ps(a8, a9), _mm256_add_ps(a10, a11)));
    float out[8];
    _mm256_storeu_ps(out, s);
    slot->sink = (uint64_t)(out[0] * 1000.0f) ^ local;
}

/* AES-128 encryption of 8 independent blocks (like CTR mode): 8 streams
 * cover the AESENC latency on cores with one or two AES units. The round keys
 * are fixed constants: the cost of AESENC does not depend on the key, so a
 * real key schedule would add nothing. 1 unit = 8 blocks x 16 B = 128 bytes,
 * 80 AES rounds. */
#define AES_BYTES_PER_UNIT 128
#define AES_ROUND(k)                                                      \
    b0 = _mm_aesenc_si128(b0, k); b1 = _mm_aesenc_si128(b1, k);           \
    b2 = _mm_aesenc_si128(b2, k); b3 = _mm_aesenc_si128(b3, k);           \
    b4 = _mm_aesenc_si128(b4, k); b5 = _mm_aesenc_si128(b5, k);           \
    b6 = _mm_aesenc_si128(b6, k); b7 = _mm_aesenc_si128(b7, k);
__attribute__((target("aes,sse2"))) static void run_aes(slot_t *slot, intptr_t tid)
{
    __m128i key[11];
    for (int r = 0; r < 11; ++r)
        key[r] = _mm_set_epi32(0x243F6A88 + r, 0x85A308D3 * (r + 1), 0x13198A2E ^ r,
                               0x03707344 + 7 * r);
    __m128i b0 = _mm_set_epi32(0, 0, (int)tid, 0), b1 = _mm_set_epi32(0, 0, (int)tid, 1);
    __m128i b2 = _mm_set_epi32(0, 0, (int)tid, 2), b3 = _mm_set_epi32(0, 0, (int)tid, 3);
    __m128i b4 = _mm_set_epi32(0, 0, (int)tid, 4), b5 = _mm_set_epi32(0, 0, (int)tid, 5);
    __m128i b6 = _mm_set_epi32(0, 0, (int)tid, 6), b7 = _mm_set_epi32(0, 0, (int)tid, 7);
    unsigned long long local = 0;

    while (!should_stop()) {
        for (int k = 0; k < g_inner; ++k) {
            b0 = _mm_xor_si128(b0, key[0]); b1 = _mm_xor_si128(b1, key[0]);
            b2 = _mm_xor_si128(b2, key[0]); b3 = _mm_xor_si128(b3, key[0]);
            b4 = _mm_xor_si128(b4, key[0]); b5 = _mm_xor_si128(b5, key[0]);
            b6 = _mm_xor_si128(b6, key[0]); b7 = _mm_xor_si128(b7, key[0]);
            AES_ROUND(key[1]) AES_ROUND(key[2]) AES_ROUND(key[3])
            AES_ROUND(key[4]) AES_ROUND(key[5]) AES_ROUND(key[6])
            AES_ROUND(key[7]) AES_ROUND(key[8]) AES_ROUND(key[9])
            b0 = _mm_aesenclast_si128(b0, key[10]); b1 = _mm_aesenclast_si128(b1, key[10]);
            b2 = _mm_aesenclast_si128(b2, key[10]); b3 = _mm_aesenclast_si128(b3, key[10]);
            b4 = _mm_aesenclast_si128(b4, key[10]); b5 = _mm_aesenclast_si128(b5, key[10]);
            b6 = _mm_aesenclast_si128(b6, key[10]); b7 = _mm_aesenclast_si128(b7, key[10]);
        }
        local += (unsigned long long)g_inner;
        atomic_store_explicit(&slot->work, local, memory_order_relaxed);
    }
    __m128i s = _mm_xor_si128(_mm_xor_si128(_mm_xor_si128(b0, b1), _mm_xor_si128(b2, b3)),
                              _mm_xor_si128(_mm_xor_si128(b4, b5), _mm_xor_si128(b6, b7)));
    slot->sink = (uint64_t)_mm_cvtsi128_si32(s) ^ local;
}
#endif

static void worker(intptr_t tid)
{
    slot_t *slot = &g_slots[tid];
    switch (g_profile) {
#ifdef HAVE_X86_ISA
    case PROFILE_AVX2: run_avx2(slot, tid); break;
    case PROFILE_AES: run_aes(slot, tid); break;
#endif
    default: run_scalar(slot, tid); break;
    }

    slot->probe_cpu = -1;
#ifdef HAVE_PROBE
    slot->probe_mhz = probe_mhz((uint64_t)(tid >= 0) | 1u);
#endif
    slot->probe_cpu = current_cpu();
}

static unsigned long long total_work(void)
{
    unsigned long long w = 0;
    for (int i = 0; i < g_threads; ++i)
        w += atomic_load_explicit(&g_slots[i].work, memory_order_relaxed);
    return w;
}

static void monitor_loop(void)
{
    double next = 1000.0;
    for (;;) {
        double e = elapsed_ms();
        if (e >= next) {
            /* Actual time of the read, not the scheduled `next`: the monitor
             * wakes up to ~30 ms late, and pairing the planned time with a
             * later work count skews the per-second rates by up to ~3 %. */
            unsigned long long w = total_work();
            printf("TICK %.1f %llu\n", elapsed_ms(), w);
            fflush(stdout);
            while (next <= e)
                next += 1000.0;
        }
        if (e >= g_seconds * 1000.0) {
            atomic_store(&g_stop, 1);
            printf("STOP %.0f %llu\n", elapsed_ms(), total_work());
            fflush(stdout);
            break;
        }
        sleep_ms(20);
    }
}

/* ---------------------------------------------------------------- threads -- */
#ifdef _WIN32
typedef HANDLE thread_t;

static DWORD WINAPI worker_entry(LPVOID arg)
{
    worker((intptr_t)arg);
    return 0;
}

static int thread_start(thread_t *t, intptr_t tid)
{
    *t = CreateThread(NULL, 0, worker_entry, (LPVOID)tid, 0, NULL);
    return *t != NULL;
}

static void thread_join(thread_t t)
{
    WaitForSingleObject(t, INFINITE);
    CloseHandle(t);
}
#else
typedef pthread_t thread_t;

static void *worker_entry(void *arg)
{
    worker((intptr_t)arg);
    return NULL;
}

static int thread_start(thread_t *t, intptr_t tid)
{
    return pthread_create(t, NULL, worker_entry, (void *)tid) == 0;
}

static void thread_join(thread_t t) { pthread_join(t, NULL); }
#endif

int main(int argc, char **argv)
{
    if (argc > 1 && strcmp(argv[1], "--caps") == 0) {
        printf("CAPS scalar%s%s\n", cpu_has_avx2_fma() ? " avx2" : "",
               cpu_has_aes() ? " aes" : "");
        return 0;
    }

    int threads = 1;
    if (argc > 1) threads = atoi(argv[1]);
    if (threads < 1) threads = 1;
    if (argc > 2) g_seconds = atof(argv[2]);
    if (g_seconds < 1.0) g_seconds = 1.0;
    if (argc > 3 && atoi(argv[3]) > 0) g_inner = atoi(argv[3]); /* 0 = default */
    if (g_inner < 16) g_inner = 16;

    const char *profile = argc > 4 ? argv[4] : "scalar";
    const char *unit = NULL;
    int per_unit = 0;
    if (strcmp(profile, "scalar") == 0) {
        g_profile = PROFILE_SCALAR;
    } else if (strcmp(profile, "avx2") == 0 && cpu_has_avx2_fma()) {
        g_profile = PROFILE_AVX2;
#ifdef HAVE_X86_ISA
        unit = "FLOPS";
        per_unit = AVX2_FLOP_PER_UNIT;
#endif
    } else if (strcmp(profile, "aes") == 0 && cpu_has_aes()) {
        g_profile = PROFILE_AES;
#ifdef HAVE_X86_ISA
        unit = "B/s";
        per_unit = AES_BYTES_PER_UNIT;
#endif
    } else {
        fprintf(stderr, "profile '%s' is not supported on this CPU\n", profile);
        printf("ERROR unsupported profile %s\n", profile);
        return 2;
    }

    g_slots = (slot_t *)alloc_aligned((size_t)threads * sizeof(slot_t));
    thread_t *th = (thread_t *)calloc((size_t)threads, sizeof(thread_t));
    if (!g_slots || !th) {
        fprintf(stderr, "out of memory\n");
        return 1;
    }

    clock_init();

#ifdef __VERSION__
    printf("BUILD %s %s\n",
#if defined(__clang__)
           "clang",
#elif defined(__GNUC__)
           "gcc",
#else
           "cc",
#endif
           __VERSION__);
#endif
    printf("WORKLOAD %d\n", WORKLOAD_VERSION);
    printf("PROFILE %s\n", profile);
    if (unit)
        printf("UNIT %s %d\n", unit, per_unit);
    printf("THREADS %d\n", threads);
    printf("START\n");
    fflush(stdout);

    int started = 0;
    for (int i = 0; i < threads; ++i) {
        if (!thread_start(&th[i], (intptr_t)i)) {
            fprintf(stderr, "failed to create thread %d\n", i);
            break;
        }
        started = i + 1;
    }
    g_threads = started;

    /* the main thread is the monitor: no extra thread competing with workers */
    if (started > 0)
        monitor_loop();
    atomic_store(&g_stop, 1);

    for (int i = 0; i < started; ++i)
        thread_join(th[i]);

    double e = elapsed_ms();
    unsigned long long w = total_work();
    uint64_t sink = 0;
    for (int i = 0; i < started; ++i)
        sink ^= g_slots[i].sink;
    for (int i = 0; i < started; ++i)
        if (g_slots[i].probe_mhz > 0)
            printf("FREQ %d %d %.0f\n", i, g_slots[i].probe_cpu, g_slots[i].probe_mhz);
    printf("DONE %.0f %llu\n", e, w);
    fflush(stdout);
    free(th);
    free_aligned(g_slots);
    g_sink = sink;
    return started > 0 ? 0 : 1;
}
