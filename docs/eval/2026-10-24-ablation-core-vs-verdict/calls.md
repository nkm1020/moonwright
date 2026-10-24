# Token Factory call log

Source `backend/out/eval_ablation/llm_log.jsonl` (not committed); 208 request(s) to 2 model(s) from 2026-10-24T10:59:51.758000+00:00 to 2026-10-24T11:39:07.444000+00:00; since 2026-10-24T10:59:43+00:00.

Every request the client sent counts once: each rung of the structured-output ladder, each re-ask and each failed request. `ok` is HTTP 200; `re-asks` are requests with `ask > 1` (the client asked again with the schema errors of the previous answer); `json format` is the `response_format` rung sent (`json_schema`, `json_object`, `none`); `thinking` whether the request let the model reason; `out tok/s` is completion tokens over request latency; latency is per request; cost is at the Token Factory list prices (`-` when a model is not priced).

## Per model and role

| model | role | calls | ok | errors (status) | re-asks | json format | thinking | tokens in (sum / mean) | tokens out (sum / mean) | out tok/s | latency p50 / p95 | cost | finish reasons |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `nvidia/Nemotron-3-Ultra-550b-a55b` | repair | 75 | 75 | 0 | 3 | json_schema 75 | on 75 | 440250 / 5870 | 1498070 / 19974 | 210 | 87.6 s / 174.0 s | $4.9345 | length 4, stop 71 |
| `nvidia/Nemotron-3-Ultra-550b-a55b` | taskgraph | 67 | 67 | 0 | 1 | json_schema 67 | on 67 | 231180 / 3450 | 791199 / 11809 | 205 | 51.7 s / 130.4 s | $2.6048 | length 1, stop 66 |
| `nvidia/Nemotron-3_5-Lightning` | structure | 66 | 66 | 0 | 0 | json_schema 66 | off 66 | 207591 / 3145 | 33728 / 511 | 114 | 2.8 s / 13.2 s | $0.0205 | stop 66 |
| **all** |  | 208 | 208 | 0 | 4 | json_schema 208 | off 66, on 142 | 879021 / 4226 | 2322997 / 11168 | 206 | 47.2 s / 154.2 s | $7.5598 | length 5, stop 203 |

## Notable requests

Non-200 answers, re-asks and requests slower than 60 s:

- 2026-10-24T11:00:52.973+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key cable_trench, attempt 1 ask 1, request edffced83294b5a0b5528a60cb470c34): 61 s
- 2026-10-24T11:01:58.383+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key access_road, attempt 2 ask 1, request fd36a4462b1c72b95b802d58fee75a99): 79 s
- 2026-10-24T11:02:04.190+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key access_road, attempt 2 ask 1, request 47644c1385c343fc7a5bb2833be3b3cc): 101 s
- 2026-10-24T11:02:27.601+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key access_road, attempt 2 ask 1, request 6aaf0b20a575745603594c5977b8d665): 104 s
- 2026-10-24T11:02:51.387+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key cable_trench, attempt 2 ask 1, request 66b9150422b56c49afd34438fa95015a): 130 s
- 2026-10-24T11:03:40.424+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key access_road, attempt 3 ask 1, request 7e825d312add0ec2c189a0dcbf3d5783): 102 s
- 2026-10-24T11:03:46.505+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key access_road, attempt 3 ask 1, request bbe14fc6c4a05942a8d4fe25e2f11d06): 79 s
- 2026-10-24T11:04:04.713+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key crater_ramp, attempt 1 ask 1, request 2a9420ab9fa61fc31804a1dd6e652354): 82 s
- 2026-10-24T11:04:33.948+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key cable_trench, attempt 4 ask 1, request cbe06418f684b59ae9fde0cb18aad31d): 64 s
- 2026-10-24T11:05:05.536+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key depleted_excavators, attempt 1 ask 1, request a1cbfc905a9f075e5682342339e8ccd8): 69 s
- 2026-10-24T11:06:09.798+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key depleted_excavators, attempt 2 ask 1, request 032df19ed45052075317ac803e5c02a2): 64 s
- 2026-10-24T11:06:14.893+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key front_loaded_excavation, attempt 1 ask 1, request a0904227946c30bac78b86894b654c33): 68 s
- 2026-10-24T11:06:45.852+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 2 ask 1, request 86ed3cde5820774082424324a557fc37): 97 s
- 2026-10-24T11:07:03.722+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key habitat_berm_tight_horizon, attempt 1 ask 1, request 7a104b450a640dfd7d0fbe1c3973c8e7): 63 s
- 2026-10-24T11:07:23.547+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 3 ask 1, request 50442e71d10584168995c55174bf1a48): 65 s
- 2026-10-24T11:08:13.468+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 3 ask 1, request 46ee00fef64304c1bbe43de937f73ac8): 88 s
- 2026-10-24T11:08:21.574+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key depleted_excavators, attempt 4 ask 1, request df00abaee01434ce179a85544acd8249): 87 s
- 2026-10-24T11:08:54.635+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 4 ask 1, request 2322d0bceaeb99b57a78b362cb7bc425): 91 s
- 2026-10-24T11:09:08.073+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key habitat_foundation, attempt 1 ask 1, request dc6a49b9fffcd30a96023db7e834cb93): 60 s
- 2026-10-24T11:09:08.469+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 2 ask 1, request 0fc13ef8bd883c9de55a2e0bc0f7e4f0): 174 s
- 2026-10-24T11:09:26.501+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key habitat_foundation, attempt 1 ask 1, request 774e8bf341a8f867a75a897f5852b1a6): 63 s
- 2026-10-24T11:10:42.574+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key isru_stockpile, attempt 1 ask 1, request 6f1d15a85bf596c14fdda18b76ba4e9e): 70 s
- 2026-10-24T11:10:43.132+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key isru_stockpile, attempt 1 ask 1, request 3946c984add23d8951ca1d7f0b4189b7): 74 s
- 2026-10-24T11:10:50.520+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 4 ask 1, request 42bdaef4b3b8f61a780972bb6446582f): 157 s
- 2026-10-24T11:11:34.431+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key landing_pad, attempt 1 ask 1, request 7947adb440bb18d27985d592867c5540): 65 s
- 2026-10-24T11:12:07.022+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key landing_pad, attempt 1 ask 1, request a77141f3a1322465a589df162f8fcda4): 81 s
- 2026-10-24T11:12:09.650+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key landing_pad, attempt 1 ask 1, request 7a4070a439d66433ab5db5a7f869f970): 76 s
- 2026-10-24T11:12:16.855+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key isru_stockpile, attempt 2 ask 1, request 01c350863509fdcb4bc84d1ff5398ec7): 94 s
- 2026-10-24T11:12:31.572+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 2 ask 2, request d8fdd2ca88ebb2260ee1936c80cd7a7d): re-ask #2; 203 s
- 2026-10-24T11:13:12.533+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key landing_pad, attempt 2 ask 1, request 1bc1ae151045ca3432ddc970425ab966): 98 s
- 2026-10-24T11:13:23.937+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key landing_pad, attempt 2 ask 1, request a6899707e3d92aed72a9268ac376a7a9): 74 s
- 2026-10-24T11:13:41.031+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 3 ask 1, request 74206644e358de93a55545d3a3f7a338): 69 s
- 2026-10-24T11:13:49.114+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key landing_pad, attempt 2 ask 1, request c2117aafed68cd7e2dad491d544cca67): 102 s
- 2026-10-24T11:14:21.181+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key landing_pad, attempt 3 ask 1, request 8fadf9e2c0c67258bfa22622d1b31a07): 69 s
- 2026-10-24T11:14:31.879+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request c8184a4a8c4186005cebce8f7df3e87d): 132 s
- 2026-10-24T11:14:41.898+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key landing_pad, attempt 3 ask 1, request dd39d138dc913b78f44dbbfca4a51f33): 78 s
- 2026-10-24T11:15:14.435+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key landing_pad, attempt 3 ask 1, request 16af6ea4c7d0b8694da0dfb57e6b667f): 85 s
- 2026-10-24T11:15:55.075+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key landing_pad, attempt 4 ask 1, request d58dde864f5579279db105424aea8c54): 73 s
- 2026-10-24T11:16:40.872+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key landing_pad, attempt 4 ask 1, request 6e3fed4252d83a8b041d7b00324adf3a): 140 s
- 2026-10-24T11:16:45.515+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 4 ask 1, request 465f307038b0934b1a6d639371fc8548): 184 s
- 2026-10-24T11:16:58.489+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 2 ask 1, request 0af3e2b3079318fa13b285e2a8ad216a): 147 s
- 2026-10-24T11:17:57.244+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key landing_pad, attempt 4 ask 1, request d16994d06bdca9314fdebe5076ab10c1): 163 s
- 2026-10-24T11:18:40.536+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key low_battery_pad, attempt 1 ask 1, request 44143d446688a1c48592cade44c7f4c6): 113 s
- 2026-10-24T11:18:43.377+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request 1e3f7e0fc5a9f0cfb895aa2c475953cd): 119 s
- 2026-10-24T11:19:04.868+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 3 ask 1, request b07fd7b804e8d9ae456de16abe01050e): 126 s
- 2026-10-24T11:19:22.156+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request d6db5223488bd0722f3b222a95f15eb6): 195 s
- 2026-10-24T11:20:15.143+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 2, request 2eab6d51f7ff146258105f9de91d3dc8): re-ask #2
- 2026-10-24T11:20:22.851+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key landing_pad, attempt 4 ask 2, request 88b86c2dde008748312ec22d80aab6ce): re-ask #2; 146 s
- 2026-10-24T11:20:44.960+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 2 ask 1, request 50fa205eb32ccade60d2dfb9fd352eba): 122 s
- 2026-10-24T11:22:16.429+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key one_surveyor_three_structures, attempt 1 ask 1, request 4ebff8eee10927b65d86f87698f6ef9b): 127 s
- 2026-10-24T11:22:52.034+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 2 ask 1, request 3c1f7aa2cae6117c11ce6cdac9778ffc): 157 s
- 2026-10-24T11:22:54.517+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key one_surveyor_three_structures, attempt 1 ask 1, request a7f53b1be6a8b27086a6a9acc5afad7d): 149 s
- 2026-10-24T11:23:04.908+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 3 ask 2, request cb6ad93682846a3647a90d76ece0d597): re-ask #2; 240 s
- 2026-10-24T11:23:22.196+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key one_surveyor_three_structures, attempt 2 ask 1, request 00b11661351c36bb0bdf3e3cf2555ad6): 66 s
- 2026-10-24T11:23:39.824+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 3 ask 1, request 6833a0d538b18ae68cd7588535f004ba): 175 s
- 2026-10-24T11:24:14.667+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 3 ask 1, request 6338f7600d185ce4001bbb10d310d4ce): 83 s
- 2026-10-24T11:24:42.839+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 4 ask 1, request 292c41a67c09ca682c6e5c968a41821f): 98 s
- 2026-10-24T11:25:15.259+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key propellant_pit_and_berm, attempt 1 ask 1, request 71dd390a863e5a767c29abc5cc8b97cc): 77 s
- 2026-10-24T11:25:22.852+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 4 ask 1, request 94f00ca929fa23556a87711b929e8f54): 68 s
- 2026-10-24T11:26:00.277+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 4 ask 1, request aa9a9d89633b520f1aae865ee09d6de1): 140 s
- 2026-10-24T11:26:24.373+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key road_and_pad, attempt 1 ask 1, request fdcdc59267275f9e8198d9c33263d2d9): 62 s
- 2026-10-24T11:27:42.107+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key small_fleet_long_berm, attempt 1 ask 1, request 27f11a9ec5837c65f1a1819cdbcf7ab7): 76 s
- 2026-10-24T11:27:43.260+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key propellant_pit_and_berm, attempt 2 ask 1, request e8e2eeebf516ebf02f965efe88c4772e): 148 s
- 2026-10-24T11:28:10.671+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key shielding_mound, attempt 3 ask 1, request 30df521c8b450652a0044c7498bc5400): 76 s
- 2026-10-24T11:29:44.172+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 2 ask 1, request 0288138b6b00f3db8851ccb4e4a7ce84): 122 s
- 2026-10-24T11:29:49.763+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key shielding_mound, attempt 4 ask 1, request a85ec4304ef3265e4db81ff082075a7e): 99 s
- 2026-10-24T11:29:57.747+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 3 ask 1, request 3789373463b1662d84078a8e9a37452e): 114 s
- 2026-10-24T11:30:23.409+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key small_fleet_long_berm, attempt 1 ask 1, request f1acc6172397303be1617328ede27b72): 166 s
- 2026-10-24T11:30:31.475+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key propellant_pit_and_berm, attempt 3 ask 1, request 145cfeb0c6f71036594ed2c740ed26d9): 168 s
- 2026-10-24T11:31:16.849+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 3 ask 1, request cdbddb6911f1b9d93f77dec84a656b2f): 93 s
- 2026-10-24T11:31:22.345+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 4 ask 1, request fc2cd67fad24d8f61ad337d8f4ee5808): 85 s
- 2026-10-24T11:31:23.216+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key solar_array_pad, attempt 1 ask 1, request 09f1ea8ad39f467e35b4708c2aad556d): 88 s
- 2026-10-24T11:32:00.809+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 2 ask 1, request e78e521d6d92923ee451ec826a485d37): 97 s
- 2026-10-24T11:32:47.949+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 2 ask 1, request 2e94924e5db273c35e38cd11693bedc4): 85 s
- 2026-10-24T11:32:56.210+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 4 ask 1, request 850dddf2bff9a53bfc8e0cc9139f4ac1): 99 s
- 2026-10-24T11:33:06.843+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 3 ask 1, request e0b6106ba4a26c1191024b05fc9ce64f): 66 s
- 2026-10-24T11:33:07.229+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 2 ask 1, request d759194b93892c07b4c0be1bc9c8eb6f): 97 s
- 2026-10-24T11:33:56.103+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 2 ask 1, request 171b8c4d415d2963dc4d4f668001aa65): 100 s
- 2026-10-24T11:34:35.919+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 3 ask 1, request 1230e85a605beda37c3ceb32a74bbc14): 89 s
- 2026-10-24T11:34:42.973+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 4 ask 1, request 7049c5433020ff43ab404cfee42e3da6): 71 s
- 2026-10-24T11:34:59.570+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 3 ask 1, request d7528e53b155789d0c343cb0f0300849): 63 s
- 2026-10-24T11:35:27.815+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 4 ask 1, request 453b170cedaa7deead035e064f403abf): 141 s
- 2026-10-24T11:35:49.780+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key tight_battery, attempt 1 ask 1, request 56bf9f26020b872580183cbbfd0e4c67): 64 s
- 2026-10-24T11:36:48.023+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 4 ask 1, request d01477ee2a961635610eabb595fe0f16): 132 s
- 2026-10-24T11:37:30.619+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key tight_battery, attempt 2 ask 1, request 3a9f1fec0c6f987ee5b2fc117abfff3a): 70 s
