// Generated protocol-value acceptance; not a production entry point.
#[test_only]
module sui_edge::protocol_value_tests {
    #[test]
    fun status_values_match_clients() {
        assert!(sui_edge::deployment_manager::status_applied() == 1, 0);
        assert!(sui_edge::deployment_manager::status_failed() == 2, 1);
    }
    #[test]
    fun fleet_settings_values_match_clients() {
        let d = sui_edge::fleet_settings::defaults();
        assert!(sui_edge::fleet_settings::check_interval_ms(&d) == 1800000, 0);
        assert!(sui_edge::fleet_settings::rollout_check_interval_ms(&d) == 60000, 1);
        assert!(sui_edge::fleet_settings::health_interval_ms(&d) == 86400000, 2);
        assert!(sui_edge::fleet_settings::archive_interval_ms(&d) == 604800000, 3);
        assert!(sui_edge::fleet_settings::archive_retention_epochs(&d) == 3, 4);
        sui_edge::fleet_settings::new(300000, 60000, 86400000, 604800000, 3);
        sui_edge::fleet_settings::new(21600000, 60000, 86400000, 604800000, 3);
        sui_edge::fleet_settings::new(1800000, 30000, 86400000, 604800000, 3);
        sui_edge::fleet_settings::new(1800000, 600000, 86400000, 604800000, 3);
        sui_edge::fleet_settings::new(1800000, 60000, 60000, 604800000, 3);
        sui_edge::fleet_settings::new(1800000, 60000, 86400000, 604800000, 3);
        sui_edge::fleet_settings::new(1800000, 60000, 86400000, 3600000, 3);
        sui_edge::fleet_settings::new(1800000, 60000, 86400000, 604800000, 3);
        sui_edge::fleet_settings::new(1800000, 60000, 86400000, 604800000, 1);
        sui_edge::fleet_settings::new(1800000, 60000, 86400000, 604800000, 53);
        sui_edge::fleet_settings::new(1800000, 0, 86400000, 604800000, 3);
    }
}
