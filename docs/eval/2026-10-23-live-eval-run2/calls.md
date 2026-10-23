# Token Factory call log

Source `out/eval_run2/llm_log.jsonl`; 328 request(s) to 3 model(s) from 2026-10-23T03:14:40.801000+00:00 to 2026-10-23T06:59:27.026000+00:00; since 2026-10-23T03:14:32+00:00.

Every request the client sent counts once: each rung of the structured-output ladder, each re-ask and each failed request. `ok` is HTTP 200; `re-asks` are requests with `ask > 1` (the client asked again with the schema errors of the previous answer); `json format` is the `response_format` rung sent (`json_schema`, `json_object`, `none`); `thinking` whether the request let the model reason; `out tok/s` is completion tokens over request latency; latency is per request; cost is at the Token Factory list prices (`-` when a model is not priced).

## Per model and role

| model | role | calls | ok | errors (status) | re-asks | json format | thinking | tokens in (sum / mean) | tokens out (sum / mean) | out tok/s | latency p50 / p95 | cost | finish reasons |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `nvidia/Nemotron-3-Ultra-550b-a55b` | repair | 55 | 55 | 0 | 4 | json_schema 55 | on 55 | 330251 / 6005 | 1047359 / 19043 | 197 | 67.8 s / 224.1 s | $3.4723 | length 4, stop 51 |
| `nvidia/Nemotron-3-Ultra-550b-a55b` | taskgraph | 66 | 66 | 0 | 0 | json_schema 66 | on 66 | 227308 / 3444 | 785950 / 11908 | 263 | 32.9 s / 85.1 s | $2.5852 | stop 66 |
| `nvidia/Nemotron-3_5-Lightning` | structure | 66 | 66 | 0 | 0 | json_schema 66 | off 66 | 207525 / 3144 | 33580 / 509 | 96 | 3.2 s / 13.5 s | $0.0205 | stop 66 |
| `nvidia/nemotron-3-super-120b-a12b` | repair | 75 | 75 | 0 | 0 | json_schema 75 | on 75 | 434045 / 5787 | 556519 / 7420 | 164 | 34.7 s / 90.9 s | $0.6311 | stop 75 |
| `nvidia/nemotron-3-super-120b-a12b` | taskgraph | 66 | 66 | 0 | 0 | json_schema 66 | on 66 | 227308 / 3444 | 682974 / 10348 | 134 | 54.6 s / 173.5 s | $0.6829 | stop 66 |
| **all** |  | 328 | 328 | 0 | 4 | json_schema 328 | off 66, on 262 | 1426437 / 4349 | 3106382 / 9471 | 181 | 36.5 s / 163.5 s | $7.3919 | length 4, stop 324 |

## Notable requests

Non-200 answers, re-asks and requests slower than 60 s:

- 2026-10-23T03:16:29.974+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key access_road, attempt 1 ask 1, request 9b69551e26dca484f9a5bc6246ca4259): 61 s
- 2026-10-23T03:19:29.477+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key crater_ramp, attempt 1 ask 1, request 454565621b8e4d425aad9091bb449b4f): 74 s
- 2026-10-23T03:20:03.825+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key crater_ramp, attempt 1 ask 1, request 2fd4c1ae7ba0be554cacfd09cd5f26ac): 110 s
- 2026-10-23T03:20:59.969+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key crater_ramp, attempt 3 ask 1, request 0e4d6a535c5274110f3addf179232f93): 70 s
- 2026-10-23T03:22:03.604+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key crater_ramp, attempt 4 ask 1, request dcb8d6a191498a1b1e36492f2356f1a9): 64 s
- 2026-10-23T03:24:01.443+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key depleted_excavators, attempt 1 ask 1, request 13a6d69ba23f4ca38f08b4551b630e81): 70 s
- 2026-10-23T03:27:22.786+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 2 ask 1, request d848563ac3aa9ce023c81d3bdfef9549): 105 s
- 2026-10-23T03:27:56.741+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 2 ask 1, request b51a71ee5901435f2434b273562216fd): 95 s
- 2026-10-23T03:29:46.917+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 3 ask 1, request 9858fe0138c2eb73e251ff774c6c243b): 194 s
- 2026-10-23T03:30:59.691+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 3 ask 1, request 72ed70811205d5bce88cf14276857b17): 217 s
- 2026-10-23T03:30:59.983+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 3 ask 1, request df1c2853e53be84f0ee1d718aa48facc): 183 s
- 2026-10-23T03:33:47.989+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 3 ask 2, request 78ce762ca184681c53b4197e86a4f56b): re-ask #2; 241 s
- 2026-10-23T03:33:52.896+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 4 ask 1, request b2590f44b657183cff9eb2c8cb2f2094): 173 s
- 2026-10-23T03:34:27.960+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 3 ask 2, request d3e56e9195492232f39a8a0682e5c79a): re-ask #2; 208 s
- 2026-10-23T03:35:03.373+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key front_loaded_excavation, attempt 1 ask 1, request 0a515a577f21a2e0f3c16b63eb01a88b): 70 s
- 2026-10-23T03:36:36.473+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 4 ask 1, request 03a22a4331fc50d8435e2f4bc39f0ff3): 168 s
- 2026-10-23T03:36:38.811+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 4 ask 1, request ec9fdad9753d49a8f434241c23767bd6): 131 s
- 2026-10-23T03:38:14.887+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 2 ask 1, request 460f74ebb5defec1c0af2440840636d1): 192 s
- 2026-10-23T03:39:41.334+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 4 ask 2, request 5dafafaa6671b4fbba413f6023841a16): re-ask #2; 185 s
- 2026-10-23T03:39:56.235+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key front_loaded_excavation, attempt 1 ask 1, request ae047740b803111abfe85641342b8e3c): 197 s
- 2026-10-23T03:41:02.044+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 4 ask 1, request 490fd173579c6fecaf56807c1274f057): 130 s
- 2026-10-23T03:41:19.299+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 2 ask 1, request 2f8bd6952ef2fdbe3a64d9f8c63eba7b): 83 s
- 2026-10-23T03:41:47.963+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key front_loaded_excavation, attempt 1 ask 1, request 6c0102d84ee677a37eb9ae77babb194f): 127 s
- 2026-10-23T03:42:34.476+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 3 ask 1, request 150cbcd9304f06a356750cbc04b84f08): 75 s
- 2026-10-23T03:44:01.575+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 4 ask 1, request 1b891f8a573ca1c81bf66b6bf461b19e): 87 s
- 2026-10-23T03:44:05.026+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key habitat_berm_tight_horizon, attempt 2 ask 1, request f35fd5351bef6b0358b392e342d97b1b): 69 s
- 2026-10-23T03:44:41.629+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 4 ask 1, request 27b115e00e84891ff77b8b458fbc019b): 93 s
- 2026-10-23T03:45:23.874+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_berm_tight_horizon, attempt 1 ask 1, request abe8bc782c7ae9cb645c27515d19ab29): 79 s
- 2026-10-23T03:46:20.343+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_foundation, attempt 1 ask 1, request 76349834288434bb4dec398a4da23a54): 61 s
- 2026-10-23T03:47:22.152+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_berm_tight_horizon, attempt 1 ask 1, request dd60fbcae4744cae6372ac5b0a3fed1a): 180 s
- 2026-10-23T03:47:24.169+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key habitat_foundation, attempt 1 ask 1, request 4557b5d02cafa13ffb6a56431dbcd301): 61 s
- 2026-10-23T03:49:23.344+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key habitat_berm_tight_horizon, attempt 2 ask 1, request ddca0e2aa55b9c3ef7169be03109a8fc): 121 s
- 2026-10-23T03:52:16.057+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key landing_pad, attempt 2 ask 1, request 6f9c4d55b7c6ba4eb1e73c542c7237ba): 88 s
- 2026-10-23T03:54:30.207+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request e02d83f08abb3f18d3adb1d6599e72c4): 86 s
- 2026-10-23T03:54:53.792+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request a10e842b94287e5f73ddd26e4cd50239): 64 s
- 2026-10-23T03:56:13.223+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 3 ask 1, request b5ee78e52cefe75de829d7213c10a635): 60 s
- 2026-10-23T03:57:21.109+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 4 ask 1, request 9d8d106f1fa4f5329b1b162206a51595): 68 s
- 2026-10-23T03:57:50.093+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request 64203fcbbebfad78055bebbe30fc70b4): 104 s
- 2026-10-23T03:58:22.709+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request 0dea6284d085b61c1cda24ba4b6fcd06): 119 s
- 2026-10-23T03:59:41.082+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request b4d8312f5f8f0ba9bfeb866063a6732a): 140 s
- 2026-10-23T04:00:44.107+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key large_fleet_pad_berm_road, attempt 4 ask 1, request 131c6115b9bdf5feb3a3f4378620e36d): 84 s
- 2026-10-23T04:00:53.818+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key large_fleet_pad_berm_road, attempt 2 ask 1, request f94284d9f86688a2627705c74cc52ee9): 73 s
- 2026-10-23T04:01:56.664+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key large_fleet_pad_berm_road, attempt 3 ask 1, request bb21b4b927bab000ab090c40678c24bf): 63 s
- 2026-10-23T04:05:32.271+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key one_surveyor_three_structures, attempt 1 ask 1, request 109af789f08782d2a66b0834c6939f29): 70 s
- 2026-10-23T04:06:49.749+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key propellant_pit_and_berm, attempt 1 ask 1, request e851847157492a38cb46da07b90fa8c4): 74 s
- 2026-10-23T04:07:52.380+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key propellant_pit_and_berm, attempt 1 ask 1, request 44d04db5fd1c1c77a87543cda43a0eba): 63 s
- 2026-10-23T04:09:55.213+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 1 ask 1, request b840d994b68d41cae0f4f085591d3a24): 127 s
- 2026-10-23T04:10:06.950+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key propellant_pit_and_berm, attempt 1 ask 1, request 5c20d86a47762eee08811f954534a06e): 154 s
- 2026-10-23T04:10:24.086+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 1 ask 1, request a59c7a90665f56ae118e033d9163ce62): 106 s
- 2026-10-23T04:11:57.137+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 1 ask 1, request af52c5b6b6b3adcff334702e552e10d8): 61 s
- 2026-10-23T04:15:04.955+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key small_fleet_long_berm, attempt 1 ask 1, request ad3fce8000d096dfbc44ac577a0b2217): 61 s
- 2026-10-23T04:15:05.591+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 2 ask 1, request 971959adbdf7c2f896b49dc3aa3765f1): 77 s
- 2026-10-23T04:15:11.250+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 2 ask 1, request 42460412b326203c0e8828fd15f99c1d): 68 s
- 2026-10-23T04:16:54.138+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 3 ask 1, request 1663762764a50ba3aa9a8cd8e08a9311): 103 s
- 2026-10-23T04:17:19.105+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 3 ask 1, request 1e72d6dfe21a8437f453b1b0d134bc60): 86 s
- 2026-10-23T04:18:36.435+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 4 ask 1, request d29b316fa8d2bc11640bfc7ba8e5f2dc): 77 s
- 2026-10-23T04:19:16.690+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 4 ask 1, request 33f2bca9d371a8ba182ee6b8cd257bd8): 143 s
- 2026-10-23T04:19:19.438+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 2 ask 1, request ee75addfa4c7347966f201f1f4cd43a0): 126 s
- 2026-10-23T04:19:41.344+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key small_fleet_long_berm, attempt 1 ask 1, request d7f47bf7161d983d074ff4b74bed1a6a): 65 s
- 2026-10-23T04:20:55.953+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key small_fleet_long_berm, attempt 2 ask 1, request 36b5382e49dec870fce18bfe854c1ae4): 61 s
- 2026-10-23T04:21:26.024+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 4 ask 1, request ee1b4f396770312115f28aac2e3524b8): 68 s
- 2026-10-23T04:22:28.491+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key solar_array_pad, attempt 1 ask 1, request 3f30dd9944db489c0cd46d032c7c1dd1): 62 s
- 2026-10-23T04:22:55.333+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 2 ask 1, request 1c232779083dccdf694091a24565d238): 80 s
- 2026-10-23T04:24:26.345+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 2 ask 1, request 251c3bbf9d3072746e7bd8415167c958): 89 s
- 2026-10-23T04:25:08.475+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 3 ask 1, request 6779319c615c7f9b5214b72b9dd9d287): 133 s
- 2026-10-23T04:26:15.179+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 3 ask 1, request cd48d2fcbccc92d59d8af22e37b76504): 109 s
- 2026-10-23T04:27:16.722+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key solar_array_pad, attempt 1 ask 1, request 170c59c646460ec0a37d735d6e6db0c1): 76 s
- 2026-10-23T04:28:47.951+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key three_pads_in_order, attempt 2 ask 1, request 38acad410845a6494937f14ffb2e9f53): 125 s
- 2026-10-23T04:42:37.540+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key tight_battery, attempt 1 ask 1, request 6cfc6aab028984ecad759d9c64cfa669): 83 s
- 2026-10-23T04:42:38.497+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key solar_array_pad, attempt 4 ask 1, request ac17720d25239a2b341f6028b265b309): 88 s
- 2026-10-23T05:11:36.174+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key tight_battery, attempt 2 ask 1, request 36ec1e6c9c3f81368c50fad386078718): 90 s
- 2026-10-23T05:50:55.915+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key tight_battery, attempt 1 ask 1, request 9f88ef73e86b4ef706e9c4b21e3e32bc): 224 s
- 2026-10-23T05:51:14.481+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key tight_battery, attempt 1 ask 1, request 9115a6228e3d2d388d95f6e3755e4cb9): 404 s
- 2026-10-23T05:51:50.339+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key three_pads_in_order, attempt 2 ask 2, request 75c2ccce229894d2d99ad621f8225f3d): re-ask #2; 548 s
- 2026-10-23T05:52:36.817+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key two_pads_one_printer, attempt 1 ask 1, request 0fa836dc6e6cfa9d80b267fe747ed7ea): 79 s
- 2026-10-23T06:39:50.861+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key two_pads_one_printer, attempt 1 ask 1, request 40fc10b3ea75e6f906e4c8027266e790): 439 s
- 2026-10-23T06:40:09.268+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key three_pads_in_order, attempt 4 ask 1, request 435b9d45e9865ae656cf2856580bb0c7): 477 s
- 2026-10-23T06:56:31.809+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key two_pads_one_printer, attempt 1 ask 1, request 66aa055b9bef404330baf4fbc7b2562e): 547 s
- 2026-10-23T06:56:49.988+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key two_pads_one_printer, attempt 1 ask 1, request 1e833114f7db23e4916e267e0b2f1d7d): 116 s
- 2026-10-23T06:56:54.850+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key three_pads_in_order, attempt 1 ask 1, request 4c02e3372ce3ab215a2955c3aad61d5a): 102 s
- 2026-10-23T06:59:07.827+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key two_pads_one_printer, attempt 1 ask 1, request a8cf2966398f841c602b7c0fc2dc7131): 65 s
