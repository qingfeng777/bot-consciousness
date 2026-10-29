# conflict · prompt 1.0.0

任务：对可能矛盾的主张提出分类和复核问题，不决定赢家。先核对实体、属性、时间区间、单位与适用条件。昨天在 A 今天在 B 是 temporal_change，100cm 与 1m 是 unit_difference，不是事实冲突。
相同信息的多次转述不提高来源独立性。证据不足用 uncertain；只有在同一条件下明确互斥才标 conflict。questions 提出可操作的复核，不输出“已解决”。
示例 data：{"assessment":"uncertain","evidence":["e1","e2"],"questions":["两条观察是否指同一时间和同一物体？"]}
