# Conclusion (auto-generated)

_Based on 855 successful runs, 855 parameter combinations, emulation mode: software._

## Headline

Across the loss x delay grid UDP wins **44** metric/condition comparisons, TCP wins **14**, and **14** are ties (within 5%). TCP always wins on **reliability** (100% delivery, in order); UDP wins on **latency, jitter and throughput as soon as the path has loss or delay**. On a clean, low-latency link the two are nearly identical.

## Where TCP throughput collapses

- **Delay 0 ms:** TCP starts at 8.29 Mbps with no loss; it never falls below 50% of that within the tested loss range (max 10%). TCP falls behind UDP from **10% loss** (7.25 vs 7.78 Mbps).
- **Delay 50 ms:** TCP starts at 7.44 Mbps with no loss; it collapses below 50% of that at **1% loss** (3.50 Mbps). TCP falls behind UDP from **0% loss** (7.44 vs 8.08 Mbps).
- **Delay 200 ms:** TCP starts at 1.79 Mbps with no loss; it collapses below 50% of that at **1% loss** (0.27 Mbps). TCP falls behind UDP from **0% loss** (1.79 vs 7.12 Mbps).
- **Delay alone (no loss):** raising delay from 0 to 200 ms changes TCP throughput 8.29 -> 1.79 Mbps (-78%) and UDP 8.27 -> 7.12 Mbps (-14%). TCP is RTT-bound (slow start, ACK clocking); UDP sends at the application's rate whatever the RTT.

## Winner per metric and condition

|   Loss % |   Delay ms | Throughput   | Mean RTT   | p95 RTT   | Jitter   | Delivery ratio   | Completion time   |
|---------:|-----------:|:-------------|:-----------|:----------|:---------|:-----------------|:------------------|
|        0 |          0 | tie          | TCP        | TCP       | tie      | tie              | TCP               |
|        0 |         50 | UDP          | UDP        | UDP       | TCP      | tie              | TCP               |
|        0 |        200 | UDP          | UDP        | UDP       | TCP      | tie              | UDP               |
|        1 |          0 | tie          | UDP        | UDP       | UDP      | tie              | TCP               |
|        1 |         50 | UDP          | UDP        | UDP       | TCP      | tie              | TCP               |
|        1 |        200 | UDP          | UDP        | UDP       | UDP      | tie              | UDP               |
|        5 |          0 | tie          | UDP        | UDP       | UDP      | tie              | TCP               |
|        5 |         50 | UDP          | UDP        | UDP       | UDP      | tie              | UDP               |
|        5 |        200 | UDP          | UDP        | UDP       | UDP      | tie              | UDP               |
|       10 |          0 | UDP          | UDP        | UDP       | UDP      | tie              | TCP               |
|       10 |         50 | UDP          | UDP        | UDP       | TCP      | TCP              | UDP               |
|       10 |        200 | UDP          | UDP        | UDP       | UDP      | TCP              | UDP               |

## UDP measured loss (%) per condition

|                |   delay 0 ms |   delay 50 ms |   delay 200 ms |
|:---------------|-------------:|--------------:|---------------:|
| 0% configured  |          0   |           0   |            0   |
| 1% configured  |          1.5 |           0   |            0.5 |
| 5% configured  |          1.5 |           3   |            4   |
| 10% configured |          5   |          13.5 |           10.5 |

UDP loss tracks the configured loss (each direction is impaired independently, so only data-direction loss counts here); TCP's measured loss is always 0% because it retransmits.

## Other findings

- **Bandwidth bottleneck (1 Mbps):** throughput is capped at the link rate (TCP 1.00 / UDP 1.01 Mbps) and the bottleneck queue inflates RTT to 787 ms (TCP) / 453 ms (UDP). UDP has no congestion control, so it kept sending above capacity and lost 37.5% of its packets to queue overflow; TCP slowed down instead and lost 0.0%.
- **Concurrency:** going from 1 to 10 clients scales aggregate throughput (TCP 8.29 -> 82.10 Mbps; UDP 8.27 -> 81.66 Mbps).
- **Packet size:** at a fixed packet rate goodput grows with size (TCP 0.40 -> 11.24 Mbps; UDP 0.39 -> 11.09 Mbps); with 64 B packets the 15 B header alone is 23% overhead (TCP adds 4 B framing).
- **Reordering:** with delay jitter UDP saw up to 159 out-of-order packets per run; TCP always delivered in order (0), at the price of head-of-line blocking.

## QoS recommendation

| Application            | Recommended   | Data-backed reason                                                                                                                                                                                                                                                              |
|:-----------------------|:--------------|:--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| File transfer          | TCP           | Every byte must arrive: TCP delivered 100.0% at 10% loss, UDP only 90.3%. Slower completion is acceptable.                                                                                                                                                                      |
| Web browsing           | TCP           | Pages must be complete and in order (TCP: 0 reordered/lost). Cost: TCP mean RTT 268 ms vs UDP 100 ms at 1% loss / 50 ms, so keep connections short-lived (or use QUIC: reliable streams over UDP).                                                                              |
| Video streaming (live) | UDP           | Needs steady rate more than perfection: at 5% loss / 50 ms UDP kept 7.75 of 8.08 Mbps (-4%) while TCP fell from 7.44 to 0.64 Mbps (-91%). Codecs/FEC hide the loss. (Buffered VOD often uses TCP/HTTP.)                                                                         |
| VoIP                   | UDP           | Late audio is useless: at 1% loss / 50 ms TCP p95 RTT was 396 ms vs UDP 107 ms (3.7x higher), because a lost TCP segment holds back every later packet. Jitter at 5% loss / 50 ms: TCP 39.90 ms vs UDP 3.04 ms. A few lost 20 ms frames are inaudible; concealment covers them. |
| Online gaming          | UDP           | Latency must stay low and predictable under loss: at 1% loss / 50 ms the mean RTT was TCP 268 ms vs UDP 100 ms (no head-of-line blocking); on a clean link they are comparable (TCP 0.214 / UDP 0.240 ms). Stale position updates can simply be dropped.                        |
