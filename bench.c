/*
 * bench.c - deterministic mixed integer/FP CPU workload for PPW measurement.
 *
 * Builds on Windows (Win32 threads, MinGW-w64) and Linux/POSIX (pthreads):
 *   gcc -O2 -static bench.c -o bench.exe            (Windows)
 *   gcc -O2 -static -pthread bench.c -o bench       (Linux)
 *
 * Usage: bench <threads> <seconds> [inner]
 *
 * Emits on stdout:
 *   BUILD <compiler version>
 *   THREADS <n>
 *   START
 *   TICK <elapsed_ms> <total_work_units>      (once per second)
 *   ...
 *   STOP <elapsed_ms> <total_work_units>      (workload ends)
 *   FREQ <thread> <cpu> <mhz>                 (one per thread, see probe)
 *   DONE <elapsed_ms> <total_work_units>
 *
 * 1 work unit = one fixed inner-loop block (INNER), identical for every thread
 * and every machine, so the score is "work units per second".
 */
#if !defined(_WIN32) && !defined(_GNU_SOURCE)
#define _GNU_SOURCE /* sched_getcpu; must precede every system header */
#endif
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdatomic.h>

#ifdef _WIN32
#include <windows.h>
#else
#include <pthread.h>
#include <sched.h>
#include <time.h>
#endif

#ifndef DEFAULT_INNER
#define DEFAULT_INNER 4096
#endif

/* One slot per worker, padded to its own cache line: a single shared atomic
 * counter makes every core fight over one line, which on many-core CPUs costs
 * both throughput and watts and skews the very thing being measured. */
typedef struct {
    _Atomic unsigned long long work;
    uint64_t sink;
    double probe_mhz; /* measured core clock right after the workload */
    int probe_cpu;    /* logical CPU the probe ran on, -1 if unknown */
    char pad[64 - sizeof(unsigned long long) - sizeof(uint64_t) - sizeof(double) - sizeof(int)];
} slot_t;

static slot_t *g_slots = NULL;
static int g_threads = 1;
static atomic_int g_stop = 0;
static int g_inner = DEFAULT_INNER;
static double g_seconds = 30.0;
static volatile uint64_t g_sink = 0;

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

/* --------------------------------------------------------------- workload -- */

/* Dependent integer chain + independent FP chains: not trivially vectorizable,
 * keeps the core busy with real ALU/FPU work instead of memory traffic. */
static void worker(intptr_t tid)
{
    slot_t *slot = &g_slots[tid];
    uint64_t x = 0x9E3779B97F4A7C15ULL * (uint64_t)(tid + 1) + 1;
    double a = 1.0 + (double)tid * 0.001;
    double b = 2.0;
    double c = 0.5;
    unsigned long long local = 0;

    while (!atomic_load_explicit(&g_stop, memory_order_relaxed)) {
        for (int k = 0; k < g_inner; ++k) {
            x = x * 6364136223846793005ULL + 1442695040888963407ULL;
            uint64_t y = x ^ (x >> 29);
            y *= 0x9E3779B97F4A7C15ULL;
            y ^= y >> 32;
            x += y;

            a = a * 1.0000001192092896 + b * 0.9999998807907104;
            b = b - c * 1e-7;
            c = c + 1e-9;
            if ((y & 0xFFFFu) == 0x1234u)
                a += (double)(y & 0xFFFFFF);
        }
        local += (unsigned long long)g_inner;
        /* single writer per slot: a relaxed store is enough */
        atomic_store_explicit(&slot->work, local, memory_order_relaxed);
    }

    /* keep the compiler from removing the chains (read after join) */
    slot->sink = (uint64_t)(a + b + c) ^ x ^ local;

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
        while (e >= next) {
            printf("TICK %.0f %llu\n", next, total_work());
            fflush(stdout);
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
    int threads = 1;
    if (argc > 1) threads = atoi(argv[1]);
    if (threads < 1) threads = 1;
    if (argc > 2) g_seconds = atof(argv[2]);
    if (g_seconds < 1.0) g_seconds = 1.0;
    if (argc > 3) g_inner = atoi(argv[3]);
    if (g_inner < 16) g_inner = 16;

    g_slots = (slot_t *)calloc((size_t)threads, sizeof(slot_t));
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
    free(g_slots);
    g_sink = sink;
    return started > 0 ? 0 : 1;
}
