# Token Factory call log

Source `out/eval_run3/llm_log.jsonl`; 325 request(s) to 3 model(s) from 2026-10-23T10:27:18.376000+00:00 to 2026-10-23T11:32:52.426000+00:00; since 2026-10-23T10:26:49+00:00.

Every request the client sent counts once: each rung of the structured-output ladder, each re-ask and each failed request. `ok` is HTTP 200; `re-asks` are requests with `ask > 1` (the client asked again with the schema errors of the previous answer); `json format` is the `response_format` rung sent (`json_schema`, `json_object`, `none`); `thinking` whether the request let the model reason; `out tok/s` is completion tokens over request latency; latency is per request; cost is at the Token Factory list prices (`-` when a model is not priced).

## Per model and role

| model | role | calls | ok | errors (status) | re-asks | json format | thinking | tokens in (sum / mean) | tokens out (sum / mean) | out tok/s | latency p50 / p95 | cost | finish reasons |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `nvidia/Nemotron-3-Ultra-550b-a55b` | repair | 57 | 57 | 0 | 3 | json_schema 57 | on 57 | 349965 / 6140 | 1056964 / 18543 | 198 | 86.9 s / 216.7 s | $3.5209 | length 3, stop 54 |
| `nvidia/Nemotron-3-Ultra-550b-a55b` | taskgraph | 68 | 68 | 0 | 2 | json_schema 68 | on 68 | 233313 / 3431 | 1008773 / 14835 | 223 | 56.1 s / 164.1 s | $3.2596 | length 2, stop 66 |
| `nvidia/Nemotron-3_5-Lightning` | structure | 67 | 67 | 0 | 1 | json_schema 67 | off 67 | 211585 / 3158 | 34415 / 514 | 138 | 3.3 s / 7.1 s | $0.0210 | stop 67 |
| `nvidia/nemotron-3-super-120b-a12b` | repair | 67 | 67 | 0 | 1 | json_schema 67 | off 1, on 66 | 407117 / 6076 | 663967 / 9910 | 153 | 48.8 s / 161.9 s | $0.7197 | length 2, stop 65 |
| `nvidia/nemotron-3-super-120b-a12b` | taskgraph | 66 | 66 | 0 | 0 | json_schema 66 | on 66 | 226350 / 3430 | 717362 / 10869 | 150 | 62.4 s / 131.5 s | $0.7135 | stop 66 |
| **all** |  | 325 | 325 | 0 | 7 | json_schema 325 | off 68, on 257 | 1428330 / 4395 | 3481481 / 10712 | 181 | 48.7 s / 164.0 s | $8.2347 | length 7, stop 318 |

## Notable requests

Non-200 answers, re-asks and requests slower than 60 s:

- 2026-10-23T10:28:22.356+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key cable_trench, attempt 1 ask 1, request 38df15caaef4e095544f191a255f09c3): 64 s
- 2026-10-23T10:28:56.646+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key access_road, attempt 1 ask 1, request ee353df16ece4f98f224a9b4e3652249): 98 s
- 2026-10-23T10:29:38.595+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key access_road, attempt 1 ask 1, request d9d1eb2420b2913bd9abc0f9b714e022): 83 s
- 2026-10-23T10:30:07.063+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key access_road, attempt 1 ask 1, request 2ce246508534e4b1b08de90ce65e375d): 70 s
- 2026-10-23T10:30:33.280+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key cable_trench, attempt 1 ask 1, request f592681350bb69249572a19b6bbdb1a7): 78 s
- 2026-10-23T10:30:52.592+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key cable_trench, attempt 2 ask 1, request 29b54e69810a6f5cd76a3593f2ae5be8): 161 s
- 2026-10-23T10:32:01.721+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key crater_ramp, attempt 1 ask 1, request 9f47579bf78a0410d0d3220c2c2d9500): 67 s
- 2026-10-23T10:32:04.286+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key crater_ramp, attempt 1 ask 1, request 526e6a72f83d9c622ddd3ff82d2a4b8b): 68 s
- 2026-10-23T10:33:41.710+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key depleted_excavators, attempt 1 ask 1, request 9324c152a4b80be979ea6599bfd1226b): 94 s
- 2026-10-23T10:34:02.657+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key depleted_excavators, attempt 1 ask 1, request 302088af520213d41c00d1f89ce6a58d): 136 s
- 2026-10-23T10:34:40.684+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key depleted_excavators, attempt 1 ask 1, request 3391d1e0ec251ed19c6c4b3ddf06136a): 126 s
- 2026-10-23T10:35:34.123+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key depleted_excavators, attempt 2 ask 1, request 7ca46b1b713d6b172d552aae9ac2df40): 91 s
- 2026-10-23T10:35:53.817+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key habitat_berm_tight_horizon, attempt 1 ask 1, request 39da52ba12ede3505b3e33368ed95c9b): 70 s
- 2026-10-23T10:37:06.086+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 2 ask 1, request 87c767f508d8492f5018ed1530d7b8e2): 147 s
- 2026-10-23T10:37:06.568+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 2 ask 1, request cd084aaa2bc3493d1731138a56a3faf1): 143 s
- 2026-10-23T10:38:59.459+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 3 ask 1, request 204f1dac6741c7cd6699c4c6a3cec10d): 113 s
- 2026-10-23T10:39:11.225+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key depleted_excavators, attempt 3 ask 1, request 104145d2badd831c32bd86fc0118809e): 217 s
- 2026-10-23T10:39:15.916+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key habitat_berm_tight_horizon, attempt 2 ask 1, request 17d40f467894ba82efebc11b68083401): 202 s
- 2026-10-23T10:40:19.638+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 2 ask 1, request e20b18bf37ba14c9c05a435e1eda02c7): 299 s
- 2026-10-23T10:40:33.159+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_berm_tight_horizon, attempt 1 ask 1, request 97b5e1b12110bc96ffcf0373c5887167): 77 s
- 2026-10-23T10:41:24.530+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 4 ask 1, request 824164d031b4d3158da2f55c8c2eb496): 145 s
- 2026-10-23T10:42:06.750+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 3 ask 1, request 5c4f45b7e5559c1169135a72c35c7850): 301 s
- 2026-10-23T10:42:23.778+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 3 ask 1, request c3d74ad28123aeeed81e988e7430f0cd): 124 s
- 2026-10-23T10:42:49.502+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key front_loaded_excavation, attempt 1 ask 1, request a81768685a2de63be328b95f4ed5f6d9): 85 s
- 2026-10-23T10:42:54.543+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key habitat_berm_tight_horizon, attempt 2 ask 1, request 65bc93705ac49a0a7f6640e145288e4c): 141 s
- 2026-10-23T10:43:26.260+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 4 ask 1, request 4bd397d2e949e6945745e6f159e885d2): 62 s
- 2026-10-23T10:43:30.707+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key depleted_excavators, attempt 4 ask 1, request cc458a02504f2a8133dde296092829a8): 97 s
- 2026-10-23T10:43:36.151+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 4 ask 1, request 6599758bb00f018fa94634331f057f56): 89 s
- 2026-10-23T10:44:48.994+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key front_loaded_excavation, attempt 1 ask 1, request bb5c9b65113243f0d62efe110be2eba0): 73 s
- 2026-10-23T10:45:02.108+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key front_loaded_excavation, attempt 1 ask 1, request 269ce856bbebd1d107ee69ec3bc046b4): 96 s
- 2026-10-23T10:45:12.677+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key habitat_berm_tight_horizon, attempt 1 ask 1, request 9b676a82d41e1adf817df5c73c6423b5): 97 s
- 2026-10-23T10:45:32.267+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key habitat_berm_tight_horizon, attempt 1 ask 1, request 06d295f9bbac0b8a546e604ce2b00dd3): 154 s
- 2026-10-23T10:45:34.352+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 2 ask 1, request 0a4cf7a1470886023cf45eb5e5610120): 165 s
- 2026-10-23T10:48:22.371+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_foundation, attempt 1 ask 1, request 0cf8da0090f014a9fe19dc049225aa1f): 130 s
- 2026-10-23T10:48:46.479+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 3 ask 1, request ca2ef5f71dd11ba35b0407fb3e502e32): 187 s
- 2026-10-23T10:49:09.655+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key habitat_berm_tight_horizon, attempt 1 ask 2, request 801ed56d185de6ea05111f641c6f3e75): re-ask #2; 217 s
- 2026-10-23T10:49:31.115+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_berm_tight_horizon, attempt 1 ask 1, request e23998c6c0543416faa1c7f584bc2a58): 170 s
- 2026-10-23T10:49:53.896+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key habitat_foundation, attempt 1 ask 1, request 9aaad818bc6d731fd035ea3ef4053787): 88 s
- 2026-10-23T10:49:58.858+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 2 ask 1, request 9ca5d04d4a430a29783559d1ed7371ba): 297 s
- 2026-10-23T10:51:30.407+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_foundation, attempt 1 ask 1, request 337752609618bf5de7c2e622626f18fe): 76 s
- 2026-10-23T10:51:59.176+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_berm_tight_horizon, attempt 1 ask 1, request 01c76663526ca6918326ac413cd40a0b): 169 s
- 2026-10-23T10:52:44.454+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key isru_stockpile, attempt 1 ask 1, request cf26f3ccc325604f55e18be49042c151): 71 s
- 2026-10-23T10:53:20.766+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key landing_pad, attempt 1 ask 1, request 2047d383400e74f5b6acf48fb64cb37f): 66 s
- 2026-10-23T10:53:33.367+00:00 `nvidia/Nemotron-3_5-Lightning` structure (key large_fleet_pad_berm_road, attempt 1 ask 2, request 8aad7baf08d9f55a3ac2a81fa120cf0d): re-ask #2
- 2026-10-23T10:53:58.916+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 2 ask 2, request 09873289cbbbf17e85860eeec3176c07): re-ask #2; 240 s
- 2026-10-23T10:54:32.264+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key landing_pad, attempt 1 ask 1, request 6394ff328aea8433dcb8ee44804a6a93): 78 s
- 2026-10-23T10:55:24.659+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request 2747b22487f7ebc955f4e55c2c53c063): 111 s
- 2026-10-23T10:55:29.361+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key landing_pad, attempt 2 ask 1, request 3ada81cc05cdaaaefbcca45fba43cdf2): 99 s
- 2026-10-23T10:55:43.529+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key landing_pad, attempt 2 ask 1, request e7f2f117a73dafd40ca40da76515a616): 71 s
- 2026-10-23T10:55:46.150+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key landing_pad, attempt 2 ask 1, request 1068e0eda9ef551ba6c68bf0fc517c7c): 145 s
- 2026-10-23T10:55:50.474+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 4 ask 1, request 177557a0efe5520f0cbd9fb33c753ef5): 105 s
- 2026-10-23T10:56:44.342+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 2 ask 1, request 1b9de9e038a91072ec2195121b2ddd78): 80 s
- 2026-10-23T10:56:47.032+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key landing_pad, attempt 1 ask 1, request 096ffba19edb32388119dbefbc416633): 61 s
- 2026-10-23T10:58:05.838+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request b1b8ce7ad548dfe7b6c84351ad65aac4): 86 s
- 2026-10-23T10:58:29.620+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request f8574991ffeaaddd7c2de8dd193d3091): 75 s
- 2026-10-23T10:59:29.057+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key low_battery_pad, attempt 1 ask 1, request 6d9e006bd882bc23eeab2e6047249360): 88 s
- 2026-10-23T10:59:30.307+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key low_battery_pad, attempt 1 ask 1, request f4ec95db58c022ac38a00743e1a9e8aa): 70 s
- 2026-10-23T10:59:59.973+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request 9263d30584def104ccdf72dc4e76c6cb): 127 s
- 2026-10-23T11:00:30.034+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key large_fleet_pad_berm_road, attempt 3 ask 1, request f04adf109eaf4b9e8d8fb2f707b5d19b): 74 s
- 2026-10-23T11:00:30.549+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request a8d19eb524464c811ccf44ad3c8e9c24): 96 s
- 2026-10-23T11:00:48.446+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key low_battery_pad, attempt 1 ask 1, request 4999fe2b9f021749cb077fcc7082114b): 62 s
- 2026-10-23T11:01:33.731+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 2 ask 1, request acf3858bcc2829960e0ae9c350aa7ac9): 94 s
- 2026-10-23T11:02:51.855+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key low_battery_pad, attempt 3 ask 1, request fe7273d932b05132b5d57216bdff9f46): 68 s
- 2026-10-23T11:03:01.014+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key large_fleet_pad_berm_road, attempt 2 ask 1, request 8edf7bdfa93506edd1025071241c8909): 150 s
- 2026-10-23T11:03:56.880+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key one_surveyor_three_structures, attempt 1 ask 1, request 6ca7622e191db5d9c65ff3763331b6db): 86 s
- 2026-10-23T11:04:04.976+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request 4b0b1930a396ba74565377cdfc0c6e33): 118 s
- 2026-10-23T11:04:30.435+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key large_fleet_pad_berm_road, attempt 3 ask 1, request 3d3dd27439da968cbca6f6f46bade1f7): 89 s
- 2026-10-23T11:04:57.411+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key one_surveyor_three_structures, attempt 1 ask 1, request b44e8adfe10f2b3acd5b2c56f46d73ff): 79 s
- 2026-10-23T11:04:57.972+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key one_surveyor_three_structures, attempt 2 ask 1, request a42a5f3035af7171bb06499807a4ff6a): 61 s
- 2026-10-23T11:05:58.220+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key one_surveyor_three_structures, attempt 1 ask 1, request 2cf082e4fdd4c3fb3ca7ddf804dbd521): 121 s
- 2026-10-23T11:06:40.017+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key large_fleet_pad_berm_road, attempt 2 ask 1, request 1955245c91a73a85b15b19e485c738c6): 155 s
- 2026-10-23T11:06:41.285+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key propellant_pit_and_berm, attempt 1 ask 1, request b13b4bdd2c3ef6e30a57fa5a9ccc4cb9): 78 s
- 2026-10-23T11:07:00.435+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key one_surveyor_three_structures, attempt 2 ask 1, request 8c1f4d94104a2779e7eec9428683b3e6): 62 s
- 2026-10-23T11:07:09.351+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key one_surveyor_three_structures, attempt 1 ask 1, request 49a6c42342041a5fb907379e04232bc4): 132 s
- 2026-10-23T11:07:50.023+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key propellant_pit_and_berm, attempt 1 ask 1, request 8506e46f2b78195f509c3acd3896e3be): 93 s
- 2026-10-23T11:08:34.016+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key propellant_pit_and_berm, attempt 1 ask 1, request cedfafb214fabe8e5eea20fd20a9c163): 113 s
- 2026-10-23T11:08:56.325+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key road_and_pad, attempt 1 ask 1, request 41543053dea4f7d11eee11a74c465719): 63 s
- 2026-10-23T11:09:44.013+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key propellant_pit_and_berm, attempt 1 ask 1, request 8e336b05246eea80a55bc31508c0b898): 101 s
- 2026-10-23T11:10:16.758+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 1 ask 1, request e1960a00a7b888598ca9ac4a1bf53d92): 80 s
- 2026-10-23T11:10:45.083+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 1 ask 1, request 658a21ac608a2b442697c00b4bad6dbf): 133 s
- 2026-10-23T11:11:19.136+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 1 ask 1, request fee2c47c9385d9571cfa29f9b16f72f2): 115 s
- 2026-10-23T11:13:09.316+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key small_fleet_long_berm, attempt 1 ask 1, request 37f381b193ec9f811ae277284087e50b): 107 s
- 2026-10-23T11:13:44.406+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key small_fleet_long_berm, attempt 1 ask 1, request f81851a0ecd16a3643b9af75f1985aad): 174 s
- 2026-10-23T11:13:53.007+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key solar_array_pad, attempt 1 ask 1, request a6179939b143113c8841e0366930413e): 124 s
- 2026-10-23T11:14:27.382+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key solar_array_pad, attempt 1 ask 1, request f2e2102a0f36fb14359bb0836963b3fe): 115 s
- 2026-10-23T11:15:03.055+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key small_fleet_long_berm, attempt 1 ask 1, request e3d87b5406d7a9fece8eee45bac8c570): 204 s
- 2026-10-23T11:15:08.271+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 2 ask 1, request 9be8022936aba59b7d5743b1355fb99f): 119 s
- 2026-10-23T11:16:33.757+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 2 ask 1, request 3b796e5a0c1ffd778d2e13f1270d2a7a): 91 s
- 2026-10-23T11:16:33.865+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key small_fleet_long_berm, attempt 1 ask 2, request a6286de51fd9e0e030831be228b85ca3): re-ask #2; 169 s
- 2026-10-23T11:17:11.750+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 3 ask 1, request 91b196f8794c0554161b348f6dd01b0e): 123 s
- 2026-10-23T11:17:29.575+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 2 ask 1, request a42cce4e75df464b5bf42c8b5c98fb79): 217 s
- 2026-10-23T11:17:34.769+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 2 ask 1, request ea155baf5106486038d48ed28ddb4c5c): 187 s
- 2026-10-23T11:17:35.329+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key small_fleet_long_berm, attempt 1 ask 1, request 2627f56f2baa8fa07f961b348cbad020): 62 s
- 2026-10-23T11:19:16.721+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 4 ask 1, request 2196e39089180441aee79e7083b4c7b6): 125 s
- 2026-10-23T11:19:25.740+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 2 ask 1, request 6f8d982477b0e9f1642522742adcfacd): 172 s
- 2026-10-23T11:19:37.675+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 2 ask 2, request 5b5f5265f348eccf495ef32dd2bd2e6f): re-ask #2; 128 s
- 2026-10-23T11:20:19.245+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key small_fleet_long_berm, attempt 1 ask 1, request 8b1f531d270db14173698092cc452f7f): 63 s
- 2026-10-23T11:20:24.386+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key small_fleet_long_berm, attempt 4 ask 1, request 3539069a055eab8a0b719ea5d20844df): 104 s
- 2026-10-23T11:20:43.566+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 2 ask 2, request b7cb6669e340ba92f91b789b76b2a6ac): re-ask #2; 78 s
- 2026-10-23T11:21:08.677+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 4 ask 1, request fbdb8748a5a5166e50202d30250e0f44): 158 s
- 2026-10-23T11:21:27.774+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 3 ask 1, request 8cec50d04de9f25e1df80c21982db2dc): 110 s
- 2026-10-23T11:21:32.536+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key solar_array_pad, attempt 1 ask 1, request 75eb6a664b0a90a7532cf0ff082c8994): 62 s
- 2026-10-23T11:22:06.973+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key small_fleet_long_berm, attempt 2 ask 1, request 47c17ea42dee9807df9271a06ccf7985): 108 s
- 2026-10-23T11:22:57.516+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 2 ask 1, request be98df30c9bbb06831e0a7c6c9372dea): 85 s
- 2026-10-23T11:23:06.461+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 3 ask 1, request 1b6e77c6ec45ae1126a94e8bb704c6f2): 143 s
- 2026-10-23T11:23:10.067+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key small_fleet_long_berm, attempt 3 ask 1, request 3d9d9aa5053a29b9308630bf9a49f7c9): 63 s
- 2026-10-23T11:23:21.838+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 4 ask 1, request 749372a4b7e2aeadff4b4606211ee136): 114 s
- 2026-10-23T11:24:31.274+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key small_fleet_long_berm, attempt 4 ask 1, request 1218758ca748af1746afe7da4214a3e3): 81 s
- 2026-10-23T11:24:33.370+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 3 ask 2, request 1ea072948d20cf9a03efc34805792338): re-ask #2; 87 s
- 2026-10-23T11:25:59.118+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key three_pads_in_order, attempt 1 ask 1, request 1885f9b20fc3760c2938f9739da46573): 70 s
- 2026-10-23T11:26:10.365+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key tight_battery, attempt 1 ask 1, request dca884563883f4efaacb819e2f5a45e2): 68 s
- 2026-10-23T11:26:22.077+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 4 ask 1, request 49bf20955cd2bd76a562f01064854a97): 109 s
- 2026-10-23T11:26:56.040+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key tight_battery, attempt 1 ask 1, request a2049d8a54f88c643122f2308c65bc5b): 100 s
- 2026-10-23T11:27:25.689+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key two_pads_one_printer, attempt 1 ask 1, request e6b3e439f71f9f52b779d5b6925959d0): 82 s
- 2026-10-23T11:28:00.136+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key small_fleet_long_berm, attempt 1 ask 1, request ea576e918e571acd29dbf877747eac75): 98 s
- 2026-10-23T11:28:00.660+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key tight_battery, attempt 1 ask 1, request 029fc3156f5f72355984e597ff41b2f6): 110 s
- 2026-10-23T11:28:15.724+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key tight_battery, attempt 1 ask 1, request a0b8bc2aa6d510cf3425211c74243ce3): 104 s
- 2026-10-23T11:29:19.496+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key two_pads_one_printer, attempt 1 ask 1, request 67a750ce4deea29e21ddcafde16acc43): 87 s
- 2026-10-23T11:29:19.763+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key tight_battery, attempt 2 ask 1, request 4617f2c60a6ccf0b27483c24b3ae61e6): 69 s
- 2026-10-23T11:29:23.870+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key tight_battery, attempt 2 ask 1, request 842dcaf31644980def1d7fdd11849808): 83 s
- 2026-10-23T11:29:46.695+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key two_pads_one_printer, attempt 1 ask 1, request 1bedae6b85f8e75c429b877c94436f76): 87 s
- 2026-10-23T11:30:16.380+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key small_fleet_long_berm, attempt 3 ask 1, request 47dac41268e970a66360f487100cd94f): 77 s
- 2026-10-23T11:31:13.473+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key two_pads_one_printer, attempt 1 ask 1, request fa1b5ed8598aaa051cf5dbe0fff34d43): 73 s
- 2026-10-23T11:31:31.568+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key two_pads_one_printer, attempt 1 ask 1, request 6122306c6769e412f08e16ae5dd07ca0): 62 s
- 2026-10-23T11:31:59.776+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key small_fleet_long_berm, attempt 4 ask 1, request 38bd301f3f8f27356be81443657d3ce5): 103 s
- 2026-10-23T11:32:24.328+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key two_pads_one_printer, attempt 2 ask 1, request dfbbdcde48cb032a52fe95c73fe7706f): 71 s
- 2026-10-23T11:32:52.426+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key two_pads_one_printer, attempt 2 ask 1, request c3ff43edb3c622840b008ab4c454b18b): 81 s
