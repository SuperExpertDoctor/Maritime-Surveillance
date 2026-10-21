/** A routine-only deadline: the worker keeps polling for human and material jobs. */
export class RoutineCooldown {
  private eligibleAt = 0;

  completed(now: number): void {
    this.eligibleAt = now + 15000;
  }

  nextRequest(now: number): { defer_routine: boolean } {
    return { defer_routine: now < this.eligibleAt };
  }
}

/** Keep the lease alive until all run work, including its completion receipt, settles. */
export async function withRunHeartbeat(operation: () => Promise<void>, heartbeat: () => Promise<void>, onError: (error: unknown) => void): Promise<void> {
  const timer = setInterval(() => { void heartbeat().catch(onError); }, 3000);
  try {
    await operation();
  } finally {
    clearInterval(timer);
  }
}
