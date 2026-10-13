# ZIP crawler test-module source index

Source: `C:\Users\vijay\Desktop\Crawler.zip`, reviewed 12 October 2026.

All 118 `Crawler/crawler/tests/test_*.py` modules were read and syntax-parsed without executing them. Counts below count named test functions, not parametrized cases, assertions inside suite wrappers, features or passed tests. Up to five function names per module are shown as orientation examples. This index is exhaustive for module names, not a claim of complete code review or parity with every source behaviour. For implementation decisions and exclusions, see [ZIP_FEATURE_REVIEW.md](ZIP_FEATURE_REVIEW.md).

| # | Source test module | Named test functions | Representative function names |
| --- | --- | --- | --- |
| 1 | `test_304_is_observable.py` | 4 | `test_the_304_counter_is_reported`, `test_it_is_distinct_from_the_expensive_verdict`, `test_a_fresh_progress_block_exposes_it_at_zero`, `test_the_engine_ticks_it_where_it_takes_the_skip` |
| 2 | `test_a_dead_pool_is_rebuilt.py` | 1 | `test_a_dead_pool_is_rebuilt_suite` |
| 3 | `test_a_failed_start_leaks_nothing.py` | 1 | `test_failed_start_suite` |
| 4 | `test_a_stuck_hold_must_not_pin_the_engine.py` | 1 | `test_a_stuck_hold_must_not_pin_the_engine_suite` |
| 5 | `test_a_wedged_crawl_must_not_pin_the_instance.py` | 12 | `test_a_second_concurrent_crawl_is_refused_on_either_route`, `test_a_dead_owner_heals_without_a_container_restart`, `test_a_wedge_is_refused_not_silently_granted`, `test_a_wedge_is_overridable_so_the_instance_never_needs_a_restart`, `test_a_long_but_progressing_crawl_is_never_called_wedged` |
| 6 | `test_abandoned_browsers_are_killed.py` | 1 | `test_abandoned_browser_suite` |
| 7 | `test_arm_lockout.py` | 5 | `test_no_token_means_open_exactly_as_before`, `test_the_right_token_is_accepted`, `test_missing_or_wrong_token_is_refused_and_starts_nothing`, `test_a_refusal_does_not_wedge_the_instance`, `test_everything_that_is_not_a_crawl_start_stays_open` |
| 8 | `test_base_href.py` | 3 | `test_base_href_wins_over_page_path`, `test_no_base_tag_still_resolves_against_the_page`, `test_empty_base_href_is_ignored` |
| 9 | `test_batch_report_reason.py` | 7 | `test_the_histogram_counts_every_reason`, `test_a_rare_reason_is_LOST_when_the_cap_bites_and_no_reason_is_asked_for`, `test_asking_for_the_rare_reason_returns_it_even_under_the_same_cap`, `test_filtering_does_NOT_rewrite_the_counts_beside_it`, `test_an_unknown_reason_returns_no_urls_rather_than_all_of_them` |
| 10 | `test_blank_idle_tabs.py` | 5 | `test_tab_is_blanked_after_each_item`, `test_tab_is_blanked_even_when_process_raises`, `test_blank_failure_never_kills_the_worker`, `test_env_escape_hatch`, `test_blanked_tab_is_the_one_process_returned` |
| 11 | `test_blocklist.py` | 13 | `test_a_listed_host_is_blocked`, `test_subdomains_are_covered`, `test_a_lookalike_domain_is_NOT_blocked`, `test_everything_else_is_untouched`, `test_no_file_means_nothing_is_blocked` |
| 12 | `test_c2.py` | 28 | `test_single_date_is_as_of_the_END_of_that_day`, `test_range_spans_both_endpoints_inclusive`, `test_as_of_picks_the_nearest_capture_at_or_before`, `test_as_of_returns_a_page_never_touched_since_2023`, `test_as_of_returns_nothing_when_the_archive_has_nothing` |
| 13 | `test_c2_display.py` | 7 | `test_card_sql_selects_archived_at`, `test_card_sql_column_count_matches_the_unpack`, `test_pg_record_carries_archived_at_through`, `test_archive_card_shows_the_snapshot_date_not_todays_date`, `test_archive_card_keeps_fetched_at_on_the_hover` |
| 14 | `test_c2_exclusive.py` | 10 | `test_engine_has_no_archive_fallback_left`, `test_engine_takes_no_wayback_flag`, `test_dead_env_var_no_longer_gates_recovery`, `test_deadhost_recovers_via_c3`, `test_deadhost_does_not_refund_budget_it_never_took` |
| 15 | `test_c2_job.py` | 15 | `test_archive_date_is_the_mode_switch`, `test_a_plain_job_is_untouched_by_the_archive_validator`, `test_malformed_date_rejected`, `test_archive_to_needs_a_start`, `test_backwards_range_rejected` |
| 16 | `test_c3_proxy.py` | 4 | `test_detects_akamai_block`, `test_real_content_not_flagged`, `test_retry_off_by_default`, `test_retry_budget_on` |
| 17 | `test_c3_status.py` | 1 | `test_c3_status_suite` |
| 18 | `test_camofox_js.py` | 1 | `test_camofox_js_parses` |
| 19 | `test_captcha.py` | 1 | `test_captcha_suite` |
| 20 | `test_captcha_ocr.py` | 1 | `test_captcha_ocr_suite` |
| 21 | `test_careful_hosts_expire.py` | 7 | `test_a_fresh_mark_is_honoured`, `test_a_stale_mark_expires`, `test_an_UNSTAMPED_row_is_treated_as_expired`, `test_the_ttl_is_configurable`, `test_clearing_a_host_still_works` |
| 22 | `test_category_stop_is_per_job.py` | 8 | `test_dropping_one_job_leaves_the_others_crawling`, `test_a_dropped_job_ends_stopped_so_its_frontier_survives`, `test_switching_the_category_back_on_revives_the_job`, `test_max_pages_still_wins_and_still_reports_max_pages`, `test_the_set_survives_the_process` |
| 23 | `test_centre_check.py` | 4 | `test_unconfigured_is_not_a_fault`, `test_dead_tunnel_blames_the_tunnel`, `test_leg_shape_is_tri_state`, `test_tcp_probe_separates_open_from_shut` |
| 24 | `test_cf_click.py` | 1 | `test_cf_click_suite` |
| 25 | `test_check_keywords.py` | 7 | `test_check_keywords_match`, `test_check_keywords_no_match`, `test_check_keywords_word_boundary_not_substring`, `test_check_keywords_error_on_unfetchable_url`, `test_check_keywords_uses_same_matcher_as_gate` |
| 26 | `test_compliance.py` | 11 | `test_in_allowed_window`, `test_careful_off_peak_now`, `test_robots_fetch_error_denies_careful`, `test_robots_fetch_error_allows_non_careful`, `test_robots_404_allows_careful` |
| 27 | `test_crawl_accounting.py` | 6 | `test_duplicate_skips_are_published_live`, `test_host_busy_giveups_are_not_reported_as_render_giveups`, `test_every_stat_key_is_unique`, `test_www_and_apex_share_one_politeness_budget`, `test_only_a_leading_www_is_stripped` |
| 28 | `test_crawler_api.py` | 10 | `test_health`, `test_crawl_returns_page_bundle`, `test_schema_exposes_contract`, `test_batch_endpoint`, `test_batch_parallel_preserves_order_and_runs_all` |
| 29 | `test_dashboard_js.py` | 1 | `test_dashboard_js_suite` |
| 30 | `test_dedup_failures.py` | 5 | `test_record_failure_and_is_gone_404`, `test_record_failure_410_immediate`, `test_success_resets_fail_count`, `test_is_gone_unknown_url`, `test_column_backfill_on_old_db` |
| 31 | `test_dedupe_corpus.py` | 9 | `test_the_shortest_url_in_a_group_is_the_one_kept`, `test_a_page_whose_bytes_are_unique_is_never_touched`, `test_a_duplicate_holding_the_only_copy_of_a_pdf_is_KEPT`, `test_a_duplicate_whose_pdf_the_survivor_also_has_IS_removed`, `test_duplicates_are_grouped_per_SITE_not_globally` |
| 32 | `test_delete_source.py` | 1 | `test_delete_source_suite` |
| 33 | `test_depth_breakdown_scales.py` | 7 | `test_one_sample_query_no_matter_how_many_depths`, `test_query_count_does_not_grow_with_depth_count`, `test_every_depth_still_reports_its_count`, `test_a_ONE_PAGE_depth_still_gets_its_examples`, `test_every_depth_is_sampled_even_at_1820_levels` |
| 34 | `test_depth_is_min_hops.py` | 9 | `test_a_shallower_sighting_lowers_a_recorded_depth`, `test_a_deeper_sighting_never_raises_a_depth`, `test_re_sighting_at_the_same_depth_writes_nothing`, `test_the_relax_is_buffered_not_written_inline`, `test_a_queued_item_picks_up_a_relaxation_that_landed_while_it_waited` |
| 35 | `test_dom_settle_cap.py` | 4 | `test_the_cap_bounds_REAL_time_not_accumulated_sleep`, `test_a_settling_page_returns_early`, `test_the_probe_does_not_serialise_the_dom`, `test_a_zero_cap_is_a_no_op` |
| 36 | `test_duplicate_breaker.py` | 7 | `test_the_real_case_trips_it`, `test_a_healthy_site_never_trips_it`, `test_it_waits_for_enough_evidence`, `test_a_zero_page_job_does_not_divide_by_zero`, `test_it_can_be_turned_off` |
| 37 | `test_edgecase.py` | 1 | `test_edgecase_suite` |
| 38 | `test_enqueue_links.py` | 20 | `test_camofox_page_enqueues_children`, `test_a_seed_that_expired_onto_a_broker_does_not_drag_in_the_marketplace`, `test_a_genuine_rebrand_redirect_still_moves_the_crawl`, `test_parking_hosts_are_matched_on_the_registered_domain`, `test_respects_max_depth` |
| 39 | `test_env_knobs_are_wired.py` | 3 | `test_every_documented_setting_is_passed_through_by_compose`, `test_the_knobs_this_test_was_written_for`, `test_the_scanner_would_catch_an_unwired_setting` |
| 40 | `test_errors.py` | 7 | `test_classify_failure_status`, `test_classify_failure_network`, `test_classify_failure_robots`, `test_policy_table`, `test_parse_retry_after` |
| 41 | `test_fetcher_spa.py` | 7 | `test_spa_bootstrap_does_not_fake_success_when_navigation_fails`, `test_spa_bootstrap_trusts_verified_navigation`, `test_403_with_html_body_triggers_render_fallback`, `test_click_or_goto_succeeds_when_link_present_on_current_page`, `test_click_or_goto_falls_back_when_link_not_on_current_page` |
| 42 | `test_fleet.py` | 9 | `test_node_id_prefers_the_operators_name`, `test_status_is_derived_not_guessed`, `test_payload_carries_what_the_dashboard_asks_for`, `test_a_never_reported_field_stays_none_not_zero`, `test_an_empty_payload_never_explodes` |
| 43 | `test_frontier_is_not_silently_dropped.py` | 7 | `test_clearing_a_big_frontier_is_logged_loudly`, `test_a_small_clear_is_still_recorded`, `test_clear_still_actually_clears`, `test_the_end_of_batch_path_keeps_a_large_frontier`, `test_the_start_of_batch_path_warns_before_dropping` |
| 44 | `test_frontier_junk_classes.py` | 9 | `test_percent_encoded_dot_collapses_onto_the_plain_path`, `test_every_unreserved_class_is_decoded`, `test_a_reserved_escape_is_kept_but_case_normalized`, `test_the_query_string_keeps_its_existing_treatment`, `test_a_malformed_escape_does_not_raise` |
| 45 | `test_generator_traps.py` | 7 | `test_generator_urls_are_refused`, `test_real_pages_are_still_crawled`, `test_pagination_is_untouched`, `test_the_frontier_purge_uses_the_enqueue_predicate`, `test_the_purge_leaves_the_seen_set_alone` |
| 46 | `test_gone_is_not_blocked.py` | 5 | `test_a_gone_page_is_not_rendered`, `test_a_BLOCKED_or_broken_page_is_still_rendered`, `test_a_transport_failure_is_still_rendered`, `test_a_healthy_page_is_still_served_by_httpx`, `test_a_thin_shell_is_still_rendered` |
| 47 | `test_hardening.py` | 5 | `test_is_tracker`, `test_doh_launch_args`, `test_stealth_toggle`, `test_dev_cache_roundtrip`, `test_host_overrides` |
| 48 | `test_harvest_click_mode.py` | 1 | `test_harvest_click_mode_noop_under_fixtures` |
| 49 | `test_harvest_link_relevance.py` | 2 | `test_harvest_default_ignores_link_text`, `test_harvest_opt_in_filters_irrelevant_links` |
| 50 | `test_harvest_seed_relevance.py` | 3 | `test_default_expands_links_even_when_seed_irrelevant`, `test_opt_in_prunes_links_when_seed_irrelevant`, `test_opt_in_keeps_links_when_seed_relevant` |
| 51 | `test_host_backoff.py` | 6 | `test_the_first_deflect_waits_about_the_base_delay`, `test_it_grows_with_repeated_deflections`, `test_it_is_capped`, `test_it_is_jittered`, `test_zero_and_negative_are_safe` |
| 52 | `test_host_pressure.py` | 18 | `test_the_trap_volume_is_cut_off`, `test_the_biggest_REAL_site_measured_is_untouched`, `test_shallow_pages_are_never_capped`, `test_the_cap_is_per_host_not_per_job`, `test_the_cap_is_wired_into_the_enqueue_path` |
| 53 | `test_host_refusal_and_progress.py` | 17 | `test_a_host_that_403s_everything_is_eventually_left_alone`, `test_one_page_served_clears_the_refusal_streak`, `test_the_refusal_breaker_is_separate_from_the_network_breaker`, `test_it_cools_off_rather_than_dying_permanently`, `test_a_slow_but_delivering_host_keeps_its_queue` |
| 54 | `test_host_slot_wait.py` | 9 | `test_a_free_host_is_acquired_immediately`, `test_a_saturated_host_raises_instead_of_waiting_for_ever`, `test_the_wait_is_far_below_the_item_watchdog`, `test_the_queue_depth_is_released_on_timeout`, `test_zero_restores_the_old_unbounded_wait` |
| 55 | `test_host_solver_survives_a_dropped_client.py` | 1 | `test_host_solver_suite` |
| 56 | `test_hostsolver.py` | 1 | `test_hostsolver_suite` |
| 57 | `test_hunt_mode.py` | 4 | `test_exhaustive_fills_defaults`, `test_focused_fills_defaults`, `test_explicit_values_survive_preset`, `test_no_hunt_mode_is_unchanged` |
| 58 | `test_impersonate_trigger.py` | 12 | `test_a_waf_refusal_status_reaches_the_ja3_tier`, `test_a_status_describing_the_RESOURCE_does_not`, `test_a_200_that_is_really_a_shell_is_worth_a_ja3_get`, `test_a_real_page_is_not_thin`, `test_a_non_200_is_not_judged_on_size` |
| 59 | `test_incremental.py` | 4 | `test_incremental_off_never_skips`, `test_fresh_depth_keeps_discovery_surface`, `test_fresh_depth_zero_only_seeds_fresh`, `test_conditional_headers_round_trip` |
| 60 | `test_ingest_loss.py` | 1 | `test_ingest_loss_suite` |
| 61 | `test_ingest_validation.py` | 6 | `test_accepts_valid_document`, `test_rule1_empty_main_text`, `test_rule1_missing_canonical_url`, `test_rule1_missing_content_hash`, `test_rule4_bad_date` |
| 62 | `test_ingested_at_is_compared_as_text.py` | 5 | `test_live_summary_does_not_cast_the_column`, `test_the_bound_is_built_from_UTC`, `test_the_bound_format_matches_how_rows_are_stored`, `test_the_index_is_in_the_schema`, `test_no_other_query_casts_ingested_at` |
| 63 | `test_instance_isolation.py` | 5 | `test_two_instances_get_two_issue_logs`, `test_two_instances_get_two_paid_proxy_counters`, `test_an_explicit_path_still_wins`, `test_single_flight_covers_the_synchronous_path`, `test_the_owner_handle_never_reaches_a_json_response` |
| 64 | `test_interaction.py` | 7 | `test_scroll_infinite_stops_when_height_stops_growing`, `test_scroll_viewport_scrolls_steps_times`, `test_pagination_merges_pages_with_break_markers`, `test_pagination_stops_when_url_unchanged`, `test_no_interaction_returns_html_unchanged` |
| 65 | `test_interaction_present.py` | 7 | `test_present_finds_existing_selector`, `test_absent_selector_is_false_and_costs_one_query`, `test_empty_selector_never_reaches_the_page`, `test_malformed_selector_is_treated_as_absent_not_raised`, `test_comma_list_is_one_selector` |
| 66 | `test_issues_host.py` | 4 | `test_a_host_is_found_by_the_name_the_dashboard_shows`, `test_the_summary_and_the_rows_agree_for_every_host`, `test_a_different_host_is_not_swept_in`, `test_no_host_filter_still_returns_everything` |
| 67 | `test_jobgen.py` | 5 | `test_candidate_pool_includes_entity_products_and_tech_domain_keywords`, `test_candidate_pool_deduped_and_no_blanks`, `test_candidate_pool_tender_uses_tender_keywords`, `test_candidate_pool_unknown_entity_is_empty_not_error`, `test_generate_default_path_unchanged_offline` |
| 68 | `test_json_pager.py` | 1 | `test_json_pager_suite` |
| 69 | `test_lanes.py` | 1 | `test_lanes_suite` |
| 70 | `test_live_facets.py` | 1 | `test_live_facets_suite` |
| 71 | `test_live_rollups_are_single_flight.py` | 7 | `test_concurrent_callers_run_the_query_ONCE`, `test_a_second_call_inside_the_ttl_is_served_from_cache`, `test_the_cache_expires`, `test_keys_do_not_share_a_cache_slot`, `test_quality_gets_a_much_longer_ttl_than_the_others` |
| 72 | `test_locale_trap.py` | 5 | `test_the_measured_trap_urls_are_refused`, `test_it_is_caught_at_the_SECOND_hop_not_the_twentieth`, `test_ordinary_localized_sites_are_still_crawlable`, `test_two_letter_segments_that_are_not_languages_do_not_count`, `test_the_guard_is_stateless_and_cheap` |
| 73 | `test_move_frontier.py` | 14 | `test_the_pending_urls_move`, `test_the_SEEN_SET_moves`, `test_learned_host_hints_move`, `test_other_jobs_on_the_same_instance_are_untouched`, `test_a_failed_copy_never_deletes_the_source` |
| 74 | `test_no_query_detoasts_the_corpus.py` | 4 | `test_no_query_reads_a_toasted_column_to_get_its_size`, `test_the_scanner_catches_a_planted_regression`, `test_docstring_skipping_does_not_hide_real_queries`, `test_the_guarded_coalesce_form_is_allowed` |
| 75 | `test_one_loop_outlives_the_batch.py` | 1 | `test_one_loop_outlives_the_batch_suite` |
| 76 | `test_paid_proxy.py` | 1 | `test_paid_proxy_suite` |
| 77 | `test_path_accumulation_trap.py` | 12 | `test_the_real_trap_urls_are_refused`, `test_a_scheme_in_the_path_is_refused`, `test_real_pages_from_the_same_batch_still_crawl`, `test_extension_no_longer_hides_a_repeat`, `test_one_repeated_segment_is_still_allowed` |
| 78 | `test_pool_reuse_with_a_real_browser.py` | 2 | `test_a_second_batch_can_use_the_first_batch_s_browsers`, `test_repeated_batches_do_not_grow_the_driver_count` |
| 79 | `test_portal_robustness.py` | 9 | `test_extract_pdf_links_broadened`, `test_extract_pdf_links_extensionless_and_download_text`, `test_extract_tables_graceful`, `test_has_any`, `test_pagination_follows_ajax_then_stops` |
| 80 | `test_probe.py` | 17 | `test_pure_site_keeps_gate_off`, `test_zero_corpus_hits_still_pure`, `test_high_offtopic_ratio_flips_to_mixed`, `test_high_ratio_on_too_few_links_does_not_flip`, `test_four_distinct_offtopic_sections_flips` |
| 81 | `test_published_date.py` | 10 | `test_jsonld_is_preferred`, `test_jsonld_graph_is_walked`, `test_broken_jsonld_does_not_lose_the_page`, `test_future_date_is_refused_everywhere`, `test_sidebar_time_is_not_the_article_date` |
| 82 | `test_push_centre.py` | 1 | `test_push_centre_suite` |
| 83 | `test_ram_brake_sees_its_own_cap.py` | 12 | `test_pressure_is_measured_against_the_cap_not_the_host`, `test_a_healthy_container_reads_low`, `test_page_cache_is_not_pressure`, `test_an_uncapped_cgroup_falls_back_to_the_host_reading`, `test_a_host_scale_v1_limit_is_not_a_cap` |
| 84 | `test_recycle_closes_dead_browsers.py` | 5 | `test_dead_browsers_are_closed_not_accumulated`, `test_repeated_recycles_do_not_grow_the_pool`, `test_a_live_context_is_reused_and_nothing_is_closed`, `test_the_crashed_tab_is_always_closed`, `test_a_close_that_hangs_does_not_block_the_replacement` |
| 85 | `test_render_admission.py` | 6 | `test_the_governor_limit_wins_when_it_is_lower`, `test_the_lane_cap_wins_when_IT_is_lower`, `test_it_never_returns_zero`, `test_a_missing_governor_falls_back_to_the_lane_cap`, `test_it_is_the_min_of_the_two` |
| 86 | `test_render_escalation.py` | 5 | `test_failures_do_not_count_as_a_js_signal`, `test_a_real_page_that_is_a_shell_DOES_count`, `test_a_non_html_body_is_not_a_js_signal`, `test_the_escalation_site_is_gated_on_it`, `test_needs_render_still_treats_a_failure_as_render_worthy` |
| 87 | `test_render_escalation_evidence.py` | 8 | `test_an_ordinary_html_page_does_NOT_escalate_its_host`, `test_a_thin_link_less_shell_DOES_escalate`, `test_an_empty_body_escalates`, `test_a_failure_is_never_evidence_about_javascript`, `test_a_pdf_never_escalates` |
| 88 | `test_render_fairness.py` | 1 | `test_render_fairness_suite` |
| 89 | `test_render_fallback.py` | 1 | `test_render_fallback_suite` |
| 90 | `test_render_is_spent_where_it_helps.py` | 9 | `test_a_rate_limit_does_not_buy_a_browser`, `test_503_STILL_RENDERS_even_though_it_shares_429s_bucket`, `test_a_server_saying_later_does_not_buy_a_browser`, `test_a_403_still_renders`, `test_a_gone_page_still_does_not_render` |
| 91 | `test_render_lane_admission.py` | 1 | `test_render_lane_admission_suite` |
| 92 | `test_render_settle.py` | 1 | `test_render_settle_suite` |
| 93 | `test_resource_blocking.py` | 5 | `test_the_expensive_types_are_blocked`, `test_script_and_document_are_never_blocked`, `test_handler_aborts_only_the_useless`, `test_a_broken_route_layer_never_costs_the_page`, `test_it_can_be_switched_off` |
| 94 | `test_resume.py` | 1 | `test_resume_suite` |
| 95 | `test_resume_refusal.py` | 4 | `test_a_refused_resume_raises_instead_of_claiming_success`, `test_a_successful_background_resume_still_reports_resumed`, `test_a_SYNCHRONOUS_resume_is_not_mistaken_for_a_refusal`, `test_an_unknown_job_still_raises_400_not_409` |
| 96 | `test_robots_compliance.py` | 1 | `test_robots_compliance_suite` |
| 97 | `test_run_to_completion.py` | 4 | `test_wall_clock_off_means_no_backstop`, `test_slot_heartbeat_fires_during_cooldown`, `test_slot_no_heartbeat_when_not_cooling`, `test_background_batch_returns_immediately` |
| 98 | `test_search.py` | 5 | `test_search_projects_no_toasted_column`, `test_body_clause_matches_the_index_expression`, `test_body_tier_off_until_index_valid`, `test_query_text_is_bound_not_formatted`, `test_empty_query_returns_nothing_and_no_search` |
| 99 | `test_seed_www_fallback.py` | 8 | `test_www_is_used_when_the_apex_does_not_resolve`, `test_a_resolving_apex_is_left_exactly_alone`, `test_when_neither_resolves_the_original_seed_is_kept`, `test_a_www_host_is_never_rewritten`, `test_the_path_and_scheme_survive` |
| 100 | `test_sipri_attachments.py` | 8 | `test_pdf_links_resolves_and_keeps_off_host_documents`, `test_an_html_error_page_served_as_a_pdf_is_never_stored`, `test_a_real_pdf_is_stored_content_addressed_with_the_right_type`, `test_the_same_pdf_from_two_pages_occupies_one_object`, `test_an_internal_address_is_refused_and_never_fetched` |
| 101 | `test_sipri_datasets.py` | 1 | `test_sipri_datasets_suite` |
| 102 | `test_sipri_store.py` | 1 | `test_sipri_store_suite` |
| 103 | `test_sipri_transfers.py` | 1 | `test_sipri_transfers_suite` |
| 104 | `test_site_gate.py` | 5 | `test_only_keyword_pages_are_captured`, `test_no_site_wide_unlock`, `test_empty_corpus_keeps_all`, `test_keyword_flag_off_captures_whole_site`, `test_keyword_flag_on_filters_per_page` |
| 105 | `test_sitemap_coverage_verdicts.py` | 8 | `test_a_real_gap_is_called_a_gap`, `test_a_partial_sitemap_is_not_blamed_on_the_crawl`, `test_two_url_spaces_for_one_site_are_flagged_not_counted`, `test_no_sitemap_is_never_reported_as_zero_percent`, `test_a_fully_covered_site_says_so` |
| 106 | `test_soup_reuse.py` | 6 | `test_the_same_string_is_parsed_once`, `test_a_different_document_is_not_served_from_cache`, `test_navigation_links_survive_a_visible_text_call`, `test_pdf_links_survive_too`, `test_visible_text_still_drops_chrome_and_never_leaks_markup` |
| 107 | `test_special_request.py` | 1 | `test_special_request_suite` |
| 108 | `test_static_text_recovery.py` | 4 | `test_build_document_accepts_static_text`, `test_static_text_bypasses_inner_text`, `test_duplicate_path_retries_before_dropping`, `test_recovery_exists_on_BOTH_duplicate_paths` |
| 109 | `test_stop_is_sticky.py` | 1 | `test_stop_is_sticky_suite` |
| 110 | `test_tender_defense.py` | 1 | `test_tender_defense_suite` |
| 111 | `test_the_pool_is_built_once.py` | 1 | `test_the_pool_is_built_once_suite` |
| 112 | `test_the_pool_outlives_the_batch.py` | 1 | `test_shared_pool_suite` |
| 113 | `test_tiered_scheduler.py` | 20 | `test_bucket_reason`, `test_ask_again_statuses_are_not_terminal`, `test_every_edgecase_verdict_reason_is_explicitly_bucketed`, `test_every_engine_reason_is_explicitly_bucketed`, `test_crawl_run_id_and_netpath_backlog` |
| 114 | `test_tiers.py` | 1 | `test_tiers_suite` |
| 115 | `test_tls_proceed_anyway.py` | 5 | `test_default_is_off_everywhere`, `test_host_list_is_normalised_not_matched_loosely`, `test_ssrf_guard_still_refuses_the_internal_targets`, `test_an_unverified_page_is_never_reported_as_a_plain_success`, `test_live_expired_cert_fails_closed_then_opens_only_for_the_listed_host` |
| 116 | `test_transport_block_impersonate.py` | 4 | `test_connection_refused_is_impersonate_worthy`, `test_a_dns_failure_is_not_a_fingerprint_problem`, `test_a_real_http_answer_is_not_a_transport_block`, `test_render_lane_routes_transport_blocks_to_impersonate` |
| 117 | `test_units.py` | 36 | `test_canonicalize_collapses_equivalents`, `test_same_site`, `test_canonicalize_never_raises_on_malformed`, `test_visible_text_strips_nav_footer`, `test_gate_drops_no_corpus_keyword` |
| 118 | `test_weak_host_watchdog.py` | 1 | `test_weak_host_watchdog_suite` |

