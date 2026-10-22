# Token Factory call log

Source `out/llm_log.jsonl`; 515 request(s) to 3 model(s) from 2026-10-22T21:16:44.612000+00:00 to 2026-10-22T22:55:52.837000+00:00; since 2026-10-22T21:16:00+00:00.

Every request the client sent counts once: each rung of the structured-output ladder, each re-ask and each failed request. `ok` is HTTP 200; `re-asks` are requests with `ask > 1` (the client asked again with the schema errors of the previous answer); `json format` is the `response_format` rung sent (`json_schema`, `json_object`, `none`); `thinking` whether the request let the model reason; `out tok/s` is completion tokens over request latency; latency is per request; cost is at the Token Factory list prices (`-` when a model is not priced).

## Per model and role

| model | role | calls | ok | errors (status) | re-asks | json format | thinking | tokens in (sum / mean) | tokens out (sum / mean) | out tok/s | latency p50 / p95 | cost | finish reasons |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `nvidia/Nemotron-3-Ultra-550b-a55b` | repair | 79 | 79 | 0 | 23 | json_schema 79 | on 79 | 484444 / 6132 | 969693 / 12275 | 276 | 48.0 s / 79.7 s | $3.3935 | length 34, stop 45 |
| `nvidia/Nemotron-3-Ultra-550b-a55b` | taskgraph | 111 | 111 | 0 | 32 | json_schema 111 | on 111 | 421689 / 3799 | 1460955 / 13162 | 295 | 46.2 s / 70.2 s | $4.8046 | length 44, stop 67 |
| `nvidia/Nemotron-3_5-Lightning` | structure | 69 | 69 | 0 | 1 | json_schema 69 | off 69 | 206989 / 3000 | 34003 / 493 | 159 | 3.1 s / 4.1 s | $0.0206 | stop 69 |
| `nvidia/nemotron-3-super-120b-a12b` | explain | 117 | 117 | 0 | 0 | json_schema 117 | off 117 | 399164 / 3412 | 30201 / 258 | 107 | 2.0 s / 4.7 s | $0.1469 | stop 117 |
| `nvidia/nemotron-3-super-120b-a12b` | repair | 44 | 44 | 0 | 4 | json_schema 44 | on 44 | 227223 / 5164 | 345528 / 7853 | 170 | 39.5 s / 92.4 s | $0.3791 | length 4, stop 40 |
| `nvidia/nemotron-3-super-120b-a12b` | taskgraph | 95 | 95 | 0 | 19 | json_schema 95 | on 95 | 327923 / 3452 | 1159105 / 12201 | 157 | 77.8 s / 121.4 s | $1.1416 | length 32, stop 63 |
| **all** |  | 515 | 515 | 0 | 79 | json_schema 515 | off 186, on 329 | 2067432 / 4014 | 3999485 / 7766 | 218 | 30.8 s / 103.6 s | $9.8863 | length 114, stop 401 |

## Notable requests

Non-200 answers, re-asks and requests slower than 60 s:

- 2026-10-22T21:18:34.764+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key access_road, attempt 1 ask 1, request 78aaaa29a9031c64a8a3a6e25b8acb5a): 74 s
- 2026-10-22T21:21:21.103+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key crater_ramp, attempt 1 ask 2, request 398b66c8a9d7ec57281c24340ca79c22): re-ask #2
- 2026-10-22T21:21:29.824+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key crater_ramp, attempt 1 ask 1, request ad614f0773bde9a4a53b9c7297c2bea0): 61 s
- 2026-10-22T21:21:51.327+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key crater_ramp, attempt 1 ask 1, request 535b01cf4c0ebfe4d861530d5bd58069): 72 s
- 2026-10-22T21:24:15.319+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key depleted_excavators, attempt 1 ask 1, request 86d66b8f8d2df71753225167b76d28e8): 71 s
- 2026-10-22T21:26:10.072+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key front_loaded_excavation, attempt 1 ask 1, request 2d5a8776370e9adbe00630fa5adabeda): 67 s
- 2026-10-22T21:26:22.321+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key front_loaded_excavation, attempt 1 ask 2, request 70373a376a1176330f7218003e40cdb9): re-ask #2
- 2026-10-22T21:26:51.608+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key front_loaded_excavation, attempt 1 ask 2, request 1a0addd54d2b83f6e54e0a8f4f5cacd1): re-ask #2
- 2026-10-22T21:27:33.758+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 3 ask 2, request 0e1563e7db9869cfc26e8ed8ce23d3bd): re-ask #2; 77 s
- 2026-10-22T21:27:36.281+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 2 ask 1, request cd20d8cd1f3e9a8427bab5fec8d520fd): 74 s
- 2026-10-22T21:28:12.078+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 2 ask 1, request e0965b0bce4a2ae9d6278d68f8ae1c6f): 80 s
- 2026-10-22T21:28:35.796+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 4 ask 1, request 4aac207d128a64f15e641f0063150919): 62 s
- 2026-10-22T21:28:57.550+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 2 ask 2, request bda847d1584f4d35aec2f5f9d10a31f5): re-ask #2; 81 s
- 2026-10-22T21:29:33.014+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 2 ask 2, request b4dfb478c8cf2402609783b67c6fdeda): re-ask #2; 81 s
- 2026-10-22T21:29:44.099+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 5 ask 1, request 79e2a7ed4671ebbfd487816982f29f9a): 68 s
- 2026-10-22T21:30:01.899+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 3 ask 1, request 0d0104502aaa7ae485650c1c02549331): 64 s
- 2026-10-22T21:31:55.556+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 2 ask 1, request 31ca4a65f692add969426a846190aba7): 77 s
- 2026-10-22T21:32:16.631+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key front_loaded_excavation, attempt 1 ask 1, request 5e80ea9a9602c0a9cd09832c13e8ded4): 98 s
- 2026-10-22T21:32:30.019+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 5 ask 2, request a41979f29b6603a6762fc8f6b6e5c91e): re-ask #2
- 2026-10-22T21:33:30.417+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 4 ask 1, request 5744366cf7f045a35fb1241faf1a9abf): 68 s
- 2026-10-22T21:33:59.886+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 2 ask 1, request 5cf36d60f2cf4e83ecbfcb1c482ddc57): 103 s
- 2026-10-22T21:34:17.900+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 6 ask 2, request e7bfdecf2b8521d4f305d477da14f82d): re-ask #2
- 2026-10-22T21:35:03.651+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 2 ask 2, request d96c6d6c9e42b747c1c8e6ca18bd92ae): re-ask #2; 64 s
- 2026-10-22T21:35:52.981+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_berm_tight_horizon, attempt 1 ask 1, request 799622591ee06f41a31b4d47f4326917): 102 s
- 2026-10-22T21:36:02.839+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key front_loaded_excavation, attempt 1 ask 1, request a75dc7f2966b7f4032938a5daa74a948): 105 s
- 2026-10-22T21:36:45.026+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_berm_tight_horizon, attempt 1 ask 1, request 603271f515dad6144502ba523eb88d5a): 67 s
- 2026-10-22T21:37:42.012+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_berm_tight_horizon, attempt 1 ask 2, request c9b75f24d537fef4fa2fcadec7cabb0d): re-ask #2; 109 s
- 2026-10-22T21:38:00.405+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 2 ask 1, request 7c51a5ff45526edc79b4cb07ff624341): 118 s
- 2026-10-22T21:38:37.928+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 2 ask 2, request 198e354298cbb3b6e860bc1566aa7784): re-ask #2
- 2026-10-22T21:38:53.257+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_berm_tight_horizon, attempt 2 ask 1, request c19f1e171f21cfa3db8ad57d167df656): 71 s
- 2026-10-22T21:39:32.198+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_berm_tight_horizon, attempt 1 ask 1, request 04e45dee992cf947bf3359bf2bd69e5e): 120 s
- 2026-10-22T21:40:52.619+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 4 ask 1, request f687c99b206b8698a228e0803b6a75ce): 81 s
- 2026-10-22T21:40:54.609+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_berm_tight_horizon, attempt 1 ask 2, request 9962ee907e2dc9634f4fc442f7f03cf9): re-ask #2; 82 s
- 2026-10-22T21:41:59.962+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key habitat_berm_tight_horizon, attempt 2 ask 1, request 17fe440dd01a391fba7a2e84da7f0bc9): 65 s
- 2026-10-22T21:42:10.356+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_foundation, attempt 1 ask 1, request 2398dc0d7880c71340cc68a9c8170cbf): 89 s
- 2026-10-22T21:43:16.827+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_foundation, attempt 1 ask 1, request 5835f54bd1925a540922b37d55f3f465): 108 s
- 2026-10-22T21:44:51.229+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key isru_stockpile, attempt 1 ask 1, request deb1191e4d3289411ea2b3c47e90f474): 77 s
- 2026-10-22T21:44:58.038+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_foundation, attempt 1 ask 2, request e56b243118f3a566d1148c5e8ca6f374): re-ask #2; 101 s
- 2026-10-22T21:45:59.463+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key landing_pad, attempt 1 ask 1, request 0324e0cca6418b0ec17a267de2e22642): 60 s
- 2026-10-22T21:46:09.415+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key landing_pad, attempt 1 ask 1, request 4bff23689d784022643b3f32e8f06f65): 65 s
- 2026-10-22T21:46:49.504+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key landing_pad, attempt 1 ask 2, request fb9421ea47f540780ad322e262b2177b): re-ask #2
- 2026-10-22T21:46:56.069+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key landing_pad, attempt 1 ask 1, request 167013e1f40b5d21eb0c9a1783c2b520): 68 s
- 2026-10-22T21:47:08.306+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key landing_pad, attempt 1 ask 2, request 9ce348133cd1ddf9ec7cc28743038ad6): re-ask #2
- 2026-10-22T21:47:26.553+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key landing_pad, attempt 1 ask 2, request 301786fb515fdd641f93bb703f33576a): re-ask #2
- 2026-10-22T21:48:36.241+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key landing_pad, attempt 1 ask 1, request c7f1fe90d4683410b7aa18362260043c): 72 s
- 2026-10-22T21:49:47.135+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key landing_pad, attempt 4 ask 2, request da44baff6e5effa006f21837b9f54bfb): re-ask #2
- 2026-10-22T21:50:11.741+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key landing_pad, attempt 1 ask 1, request fe65f54cbc5ceb2fc52c287f5e0bb64d): 102 s
- 2026-10-22T21:50:26.598+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 2, request 071cedf938a25bc31416406472559a54): re-ask #2
- 2026-10-22T21:51:20.965+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key landing_pad, attempt 1 ask 1, request 57308d1061f4336ed79d2d363384aead): 94 s
- 2026-10-22T21:52:01.184+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key large_fleet_pad_berm_road, attempt 2 ask 2, request 366db35f11cc97eea6dabfb1383f6a61): re-ask #2
- 2026-10-22T21:52:41.648+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request e425a1578ea069b192aeff441990679a): 64 s
- 2026-10-22T21:53:14.054+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key large_fleet_pad_berm_road, attempt 3 ask 1, request 34b995729c11e6e4d88ef44f696acbdb): 73 s
- 2026-10-22T21:53:14.416+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request e3386c3429fef814da7089d18cff0d58): 74 s
- 2026-10-22T21:53:52.293+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 2, request 055d05afff52c82079412fe552df0335): re-ask #2; 71 s
- 2026-10-22T21:54:19.062+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 2, request 55891b8345e98113427bd44725fb93a4): re-ask #2; 65 s
- 2026-10-22T21:54:19.160+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key large_fleet_pad_berm_road, attempt 3 ask 2, request 03241200102581cb87daca5b91239979): re-ask #2; 65 s
- 2026-10-22T21:54:54.759+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key large_fleet_pad_berm_road, attempt 2 ask 1, request d47a07f980b092f71ef3002d585458e2): 62 s
- 2026-10-22T21:55:26.019+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 3 ask 2, request acc145e785cb525603c5717145acfab5): re-ask #2
- 2026-10-22T21:55:32.790+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key large_fleet_pad_berm_road, attempt 2 ask 2, request 124170cbda6de93ba324f4acc1df629e): re-ask #2
- 2026-10-22T21:56:18.138+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request 5281e575587f1dd9965fcce25a91b2ab): 119 s
- 2026-10-22T21:56:47.037+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 4 ask 2, request 4ff97e0fa6c7109a4b06055bb6bf4b1b): re-ask #2
- 2026-10-22T21:57:55.626+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 2, request 66c188118bc025450d0213869d26046f): re-ask #2; 97 s
- 2026-10-22T21:58:04.911+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 5 ask 2, request 7a9ddcf7b00f0957d6dc169acaaffa3d): re-ask #2
- 2026-10-22T21:59:19.093+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request 594d814e4895f708f4072edbdf12110b): 106 s
- 2026-10-22T21:59:47.782+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 2 ask 1, request 4e65f5ab25de8f5ed94144b05a14dbea): 112 s
- 2026-10-22T22:00:58.564+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request ee8e7466db8ae4bf6831eddc10c5232e): 129 s
- 2026-10-22T22:01:28.244+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 2, request 50ee1285ba9bd3a8dca3453771bcd707): re-ask #2; 129 s
- 2026-10-22T22:01:56.282+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 2 ask 2, request 9e6edac90fd63eb8a80d3a47f2936654): re-ask #2; 128 s
- 2026-10-22T22:03:02.844+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 2, request 393431c9d9e92bc8f888bdb07a779f88): re-ask #2; 124 s
- 2026-10-22T22:03:26.512+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 2 ask 1, request 35b0684eeb7a811b5eb88db031a13962): 118 s
- 2026-10-22T22:03:52.829+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 3 ask 1, request 6bc29d2569245b7f644fa77a3072b773): 117 s
- 2026-10-22T22:04:22.301+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 2 ask 1, request a0faccdd7bfc34737c0e50d9b763a3ec): 79 s
- 2026-10-22T22:04:54.226+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 2 ask 2, request d6c1dd8f7b4746cd42a9d5c8a82f94e7): re-ask #2; 88 s
- 2026-10-22T22:05:40.786+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 3 ask 2, request fc7d58cd577f27bccbb9dd4f0c8b490a): re-ask #2; 108 s
- 2026-10-22T22:06:33.696+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 3 ask 1, request 61977828debc99cf1e4179fae793765e): 99 s
- 2026-10-22T22:07:15.349+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key low_battery_pad, attempt 1 ask 2, request 9d17c34360212240bd1be598632d294f): re-ask #2
- 2026-10-22T22:08:16.921+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 3 ask 2, request f2bf9b42a1cae714c6487c3a86169a8d): re-ask #2; 103 s
- 2026-10-22T22:08:17.859+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key low_battery_pad, attempt 1 ask 2, request 739701df70436503ca52b5d28c0288b8): re-ask #2
- 2026-10-22T22:09:03.218+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key low_battery_pad, attempt 1 ask 1, request 058bd5cecf3c9e52143b419a4858c28d): 95 s
- 2026-10-22T22:10:43.637+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key low_battery_pad, attempt 1 ask 1, request 3c772e855d7631bdaa22cf24f88ac254): 104 s
- 2026-10-22T22:12:11.830+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key one_surveyor_three_structures, attempt 1 ask 1, request 6e67323190d8769a95a02b40c034f3eb): 78 s
- 2026-10-22T22:12:39.214+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key one_surveyor_three_structures, attempt 1 ask 1, request 7e31e304599f24457bf05a2f2d5f163c): 85 s
- 2026-10-22T22:15:01.757+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key propellant_pit_and_berm, attempt 1 ask 1, request 980bbb4068229e31ae53f3b174ea8919): 66 s
- 2026-10-22T22:15:10.100+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key road_and_pad, attempt 1 ask 1, request a2499896d12ec4dd7034b3e74a79c56c): 69 s
- 2026-10-22T22:15:39.744+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key road_and_pad, attempt 1 ask 1, request 3bf02bb44a6472abffb536735f73959e): 76 s
- 2026-10-22T22:15:49.658+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key propellant_pit_and_berm, attempt 1 ask 2, request 68fa267bf331d2d4810f7f0c954ce41d): re-ask #2
- 2026-10-22T22:16:18.796+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key road_and_pad, attempt 1 ask 2, request 623247a8dc65dc611a81cf4833ae1471): re-ask #2; 69 s
- 2026-10-22T22:16:38.250+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key road_and_pad, attempt 1 ask 2, request 1616b0c2db7ac23f916f153daa1fc562): re-ask #2
- 2026-10-22T22:17:29.631+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key propellant_pit_and_berm, attempt 1 ask 1, request b4c16ac7f19c6ed948744a02446c5224): 98 s
- 2026-10-22T22:18:05.899+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key road_and_pad, attempt 2 ask 2, request 184deaf0c49282643268299c5c11654d): re-ask #2
- 2026-10-22T22:18:39.190+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key propellant_pit_and_berm, attempt 1 ask 2, request a02317896ead643fc36c6ff702bb171b): re-ask #2; 70 s
- 2026-10-22T22:18:51.329+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 1 ask 1, request 47758c634a56839fe1ef0e1f7228ead2): 91 s
- 2026-10-22T22:19:38.400+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key road_and_pad, attempt 3 ask 2, request 9d8f47850d0c725f7f4ffc73d785a9f3): re-ask #2
- 2026-10-22T22:19:50.487+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key road_and_pad, attempt 1 ask 1, request bdfa48e682527acff1d7fcb3d660bf3f): 65 s
- 2026-10-22T22:20:53.541+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key road_and_pad, attempt 1 ask 2, request 76ee7c45b9bfcc455dc03600fb16338e): re-ask #2; 63 s
- 2026-10-22T22:21:48.200+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key shielding_mound, attempt 1 ask 1, request 67e0f5e2b39c390af0a41ee0ebc3582c): 74 s
- 2026-10-22T22:21:55.404+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key road_and_pad, attempt 2 ask 1, request b6bddc3d84f178d9355c842ee5319f67): 62 s
- 2026-10-22T22:21:57.934+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key road_and_pad, attempt 4 ask 2, request ae79bca0527c97d393fbfcc948031b54): re-ask #2; 80 s
- 2026-10-22T22:22:36.652+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key shielding_mound, attempt 1 ask 2, request d70e5e02b19e3a116437382d6b55bab2): re-ask #2
- 2026-10-22T22:23:00.334+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key road_and_pad, attempt 3 ask 1, request ab3d49143c39498308400dfa04d46894): 65 s
- 2026-10-22T22:23:55.570+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key road_and_pad, attempt 3 ask 2, request 011f1f151c8ae3c61da64961b500fdf8): re-ask #2
- 2026-10-22T22:25:24.403+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 1 ask 1, request f768b932390d0b97a5f6ec0c6f9c097f): 108 s
- 2026-10-22T22:25:37.171+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key road_and_pad, attempt 5 ask 2, request 1145d979fee3fc5b72647b9c99687891): re-ask #2
- 2026-10-22T22:25:52.214+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key shielding_mound, attempt 1 ask 1, request dec94075c8d2e8823f3fbb36d48aba5d): 70 s
- 2026-10-22T22:26:10.099+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key landing_pad, attempt 1 ask 2, request 0391e0d636bcfb25485d3f8dba45e7c3): re-ask #2; 65 s
- 2026-10-22T22:26:37.588+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key shielding_mound, attempt 1 ask 2, request b57f741e5beab6ae17674f3d61fce619): re-ask #2
- 2026-10-22T22:27:08.733+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 1 ask 2, request 42eada6045d0a34e5102eb57757f6870): re-ask #2; 104 s
- 2026-10-22T22:27:49.922+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key landing_pad, attempt 3 ask 2, request 1a514bf2d6edbf7ba3cc93464be6ebc3): re-ask #2
- 2026-10-22T22:28:20.026+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key shielding_mound, attempt 1 ask 1, request 3cd1d213d66adf3afb93f1f3a22f527a): 100 s
- 2026-10-22T22:28:34.013+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 1 ask 1, request d453e69252dfa4e7a509f3b4766a72ab): 120 s
- 2026-10-22T22:29:12.389+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 2 ask 1, request 96c196efadf3ffd4eca1e151cfde7055): 124 s
- 2026-10-22T22:30:03.801+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key small_fleet_long_berm, attempt 1 ask 2, request 502fcbdb04a7b2f8dff983e834ad4ffc): re-ask #2
- 2026-10-22T22:30:29.139+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 1 ask 2, request 9a9283e8db47d3cc18afac5945a32a04): re-ask #2; 115 s
- 2026-10-22T22:31:01.299+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 2 ask 2, request 66b2ced9c27ec1377b5e1a4fd01c8c83): re-ask #2; 109 s
- 2026-10-22T22:31:16.111+00:00 `nvidia/Nemotron-3_5-Lightning` structure (key brief_cd2c705367, attempt 1 ask 2, request 4ec959f3bd7a6f705e5416164aa04d23): re-ask #2
- 2026-10-22T22:31:32.985+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key small_fleet_long_berm, attempt 2 ask 2, request 59134f513f97720309b4583f31c48368): re-ask #2
- 2026-10-22T22:32:18.159+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 2 ask 1, request 18aeab9e3daf0edca02207efacdfa146): 109 s
- 2026-10-22T22:32:43.728+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key small_fleet_long_berm, attempt 1 ask 1, request cd4675884f051016a51b88d1ceb084fe): 68 s
- 2026-10-22T22:32:49.713+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 3 ask 1, request c10c56ea7c677fbd7007bafd9acbd280): 108 s
- 2026-10-22T22:33:10.907+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key brief_cd2c705367, attempt 1 ask 2, request 56700713dedd0974a8f526742729c864): re-ask #2
- 2026-10-22T22:34:01.676+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 2 ask 2, request 76644358ff4d75f092ded641c40c729c): re-ask #2; 104 s
- 2026-10-22T22:34:12.012+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key small_fleet_long_berm, attempt 1 ask 2, request b4629da0f4a0def141e66e5e8cb05148): re-ask #2
- 2026-10-22T22:34:36.533+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 3 ask 2, request f97493a7718b6c1c3d841cff39695017): re-ask #2; 107 s
- 2026-10-22T22:34:45.239+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key brief_cd2c705367, attempt 2 ask 2, request cf140303d968225dd2bd496a0f1d01d8): re-ask #2
- 2026-10-22T22:35:30.698+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key small_fleet_long_berm, attempt 1 ask 1, request 1e366289cfb75da7f849acea9f3961a4): 77 s
- 2026-10-22T22:35:42.558+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 3 ask 1, request cbb243903dc8de68aa220b3dfaf836c0): 101 s
- 2026-10-22T22:36:33.686+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key small_fleet_long_berm, attempt 1 ask 1, request 392e3c08e99c5f8ca0cb05bae8393542): 79 s
- 2026-10-22T22:36:34.159+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 3 ask 2, request faefe30ee8f35a2c84ca9ab47d6d2906): re-ask #2
- 2026-10-22T22:36:34.500+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key brief_cd2c705367, attempt 3 ask 2, request a1578872bdf8ae67eeecddd9f00225a3): re-ask #2
- 2026-10-22T22:37:17.989+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 2 ask 1, request f720d0ff62d52a583cf67a34c66c2070): 63 s
- 2026-10-22T22:37:39.888+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key brief_cd2c705367, attempt 4 ask 1, request 0dcd69d369dd15b0e158956a4d647a21): 65 s
- 2026-10-22T22:37:49.318+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key road_and_pad, attempt 4 ask 1, request 1d7eb5c91d3926c2437856c2679077cf): 75 s
- 2026-10-22T22:37:58.304+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key solar_array_pad, attempt 1 ask 2, request f76b4c19bba9f44536a525367d135699): re-ask #2
- 2026-10-22T22:38:18.687+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 2 ask 2, request f08306edd3ca067e25f0de9cdae36d5a): re-ask #2; 61 s
- 2026-10-22T22:38:48.620+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key brief_cd2c705367, attempt 4 ask 2, request 80de7787b2b9b7e8d4e317e2825b3729): re-ask #2; 69 s
- 2026-10-22T22:39:17.908+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 2 ask 1, request eae4a5bedc1001f6ce6d9913884120c8): 80 s
- 2026-10-22T22:39:36.833+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 3 ask 1, request 729c4c6559a179fd420c40ae899bc7d6): 78 s
- 2026-10-22T22:39:45.144+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key road_and_pad, attempt 6 ask 1, request 86e95e9c9e7ad8348f20d1328f958fe1): 76 s
- 2026-10-22T22:39:49.436+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 2 ask 2, request 0dc1e6ba310f1338118d3e5e7908fdaf): re-ask #2
- 2026-10-22T22:40:03.975+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key brief_cd2c705367, attempt 5 ask 1, request 52dda5157a92a0273311b5d378936ccc): 75 s
- 2026-10-22T22:40:40.294+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key road_and_pad, attempt 6 ask 2, request ffe01a88d608b46a6922326e0395f0e9): re-ask #2
- 2026-10-22T22:40:41.813+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 3 ask 2, request d5228afc3932f9d2ba97334262e57346): re-ask #2; 65 s
- 2026-10-22T22:41:03.653+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 3 ask 1, request a75dfb3b2d46bf5366e8ff323479d61e): 74 s
- 2026-10-22T22:41:18.591+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key brief_cd2c705367, attempt 5 ask 2, request 7a9b279cffa4bcc8e7ff08ebbc13f3fc): re-ask #2; 75 s
- 2026-10-22T22:41:54.154+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 3 ask 2, request 7454b387055b71139d2c79779eff0d63): re-ask #2
- 2026-10-22T22:42:24.527+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key solar_array_pad, attempt 1 ask 2, request 5d5bc6a38b4f902ecee20e7ef2d7fc9e): re-ask #2
- 2026-10-22T22:43:30.509+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 4 ask 2, request 084a275fe12cfc03bad34a1e566e0f0d): re-ask #2
- 2026-10-22T22:44:15.047+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 3 ask 2, request eed7dc5c7357a2132c42aaf032a9f928): re-ask #2
- 2026-10-22T22:45:39.362+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key solar_array_pad, attempt 2 ask 1, request a0935945d46ae1cb922b80d9a4b431a2): 93 s
- 2026-10-22T22:45:40.334+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key three_pads_in_order, attempt 1 ask 1, request 9e37c55392b8027d6c1d610b03030228): 93 s
- 2026-10-22T22:45:59.794+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key solar_array_pad, attempt 1 ask 1, request fb61d77cb9c8af1fae6b5ba03526b49c): 102 s
- 2026-10-22T22:46:35.129+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key solar_array_pad, attempt 1 ask 2, request 40da17c3fd71a35d125b0af9c8b6b7b9): re-ask #2
- 2026-10-22T22:46:44.315+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key solar_array_pad, attempt 2 ask 2, request c8166b8d2ad8308fbc2589341fd77aef): re-ask #2; 65 s
- 2026-10-22T22:48:05.166+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key solar_array_pad, attempt 2 ask 1, request dd07fae6f85b5074b6a8d64d20f5c7e6): 90 s
- 2026-10-22T22:48:58.754+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key tight_battery, attempt 1 ask 1, request a82299cad93e4e2f6f262a016888a041): 75 s
- 2026-10-22T22:49:10.148+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key solar_array_pad, attempt 3 ask 1, request d8f3b39a1f9b8f8cd80e4ec40a9e326a): 65 s
- 2026-10-22T22:49:55.354+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key tight_battery, attempt 1 ask 1, request 8a1ba668244d944494feb1c19890e93b): 73 s
- 2026-10-22T22:50:33.631+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key two_pads_one_printer, attempt 1 ask 1, request 68b7b163434a3ec7c51f63082da7c94e): 61 s
- 2026-10-22T22:51:02.664+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key two_pads_one_printer, attempt 1 ask 2, request e08b171c659784f3405d55c1aaeb7f07): re-ask #2
- 2026-10-22T22:51:08.361+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key tight_battery, attempt 1 ask 2, request a72e0ae7932b34a32e8631107ea9ccb8): re-ask #2; 70 s
- 2026-10-22T22:52:14.718+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key two_pads_one_printer, attempt 1 ask 1, request bd0d602940e113d3ec6f5987a6dc71e3): 70 s
- 2026-10-22T22:52:43.813+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key tight_battery, attempt 2 ask 2, request 24dc9cc6f63d9f2e6369d4078578281f): re-ask #2
- 2026-10-22T22:53:58.738+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key two_pads_one_printer, attempt 1 ask 1, request 841d41c41737cab4c3183ae3358b5af5): 67 s
- 2026-10-22T22:54:45.225+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key tight_battery, attempt 1 ask 1, request 9d97b1bb867278b9e7cf8673659ec776): 120 s
- 2026-10-22T22:55:50.975+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key tight_battery, attempt 1 ask 2, request ebde24eda600cb712df69352d3b1ed6f): re-ask #2; 66 s

