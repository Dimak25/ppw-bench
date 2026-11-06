/*
 * bench.c - deterministic mixed integer/FP CPU workload for PPW measurement.
 *
 * Usage: bench.exe <threads> <seconds> [inner]
 *
 * Emits on stdout:
 *   THREADS <n>
 *   START
 *   TICK <elapsed_ms> <total_work_units>      (once per second)
 *   ...
 *   DONE <elapsed_ms> <total_work_units>
 *
 * 1 work unit = one fixed inner-loop block (INNER), identical for every thread
 * and every machine, so the score is "work units per second".
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdatomic.h>
#include <windows.h>

#ifndef DEFAULT_INNER
#define DEFAULT_INNER 4096
#endif

static _Atomic unsigned long long g_work = 0;
static volatile LONG g_stop = 0;
static volatile uint64_t g_sink = 0;
static LARGE_INTEGER g_t0, g_freq;
static int g_inner = DEFAULT_INNER;
static double g_seconds = 30.0;

static double elapsed_ms(void)
{
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    return (double)(now.QuadPart - g_t0.QuadPart) * 1000.0 / (double)g_freq.QuadPart;
}

/* Dependent integer chain + independent FP chains: not trivially vectorizable,
 * keeps the core busy with real ALU/FPU work instead of memory traffic. */
static void worker(intptr_t tid)
{
    uint64_t x = 0x9E3779B97F4A7C15ULL * (uint64_t)(tid + 1) + 1;
    double a = 1.0 + (double)tid * 0.001;
    double b = 2.0;
    double c = 0.5;
    unsigned long long local = 0;

    while (!g_stop) {
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
        atomic_fetch_add_explicit(&g_work, (unsigned long long)g_inner,
                                  memory_order_relaxed);
    }

    /* keep the compiler from removing the chains */
    g_sink += (uint64_t)(a + b + c) ^ x ^ local;
}

static DWORD WINAPI worker_thread(LPVOID arg)
{
    worker((intptr_t)arg);
    return 0;
}

static DWORD WINAPI monitor(LPVOID arg)
{
    (void)arg;
    double next = 1000.0;
    for (;;) {
        double e = elapsed_ms();
        while (e >= next) {
            unsigned long long w =
                atomic_load_explicit(&g_work, memory_order_relaxed);
            printf("TICK %.0f %llu\n", next, w);
            fflush(stdout);
            next += 1000.0;
        }
        if (e >= g_seconds * 1000.0) {
            InterlockedExchange(&g_stop, 1);
            break;
        }
        Sleep(20);
    }
    return 0;
}

int main(int argc, char **argv)
{
    int threads = 1;
    if (argc > 1) threads = atoi(argv[1]);
    if (threads < 1) threads = 1;
    if (argc > 2) g_seconds = atof(argv[2]);
    if (g_seconds < 1.0) g_seconds = 1.0;
    if (argc > 3) g_inner = atoi(argv[3]);
    if (g_inner < 16) g_inner = 16;

    QueryPerformanceFrequency(&g_freq);
    QueryPerformanceCounter(&g_t0);

    printf("THREADS %d\n", threads);
    printf("START\n");
    fflush(stdout);

    HANDLE mon = CreateThread(NULL, 0, monitor, NULL, 0, NULL);

    HANDLE *th = (HANDLE *)calloc((size_t)threads, sizeof(HANDLE));
    if (!th) return 1;
    for (int i = 0; i < threads; ++i) {
        th[i] = CreateThread(NULL, 0, worker_thread, (LPVOID)(intptr_t)i, 0, NULL);
        if (!th[i]) {
            fprintf(stderr, "failed to create thread %d\n", i);
            threads = i;
            break;
        }
    }
    if (threads > 0)
        WaitForMultipleObjects((DWORD)threads, th, TRUE, INFINITE);
    for (int i = 0; i < threads; ++i)
        CloseHandle(th[i]);
    free(th);

    InterlockedExchange(&g_stop, 1);
    if (mon) {
        WaitForSingleObject(mon, 5000);
        CloseHandle(mon);
    }

    double e = elapsed_ms();
    unsigned long long w = atomic_load_explicit(&g_work, memory_order_relaxed);
    printf("DONE %.0f %llu\n", e, w);
    fflush(stdout);
    return 0;
}
