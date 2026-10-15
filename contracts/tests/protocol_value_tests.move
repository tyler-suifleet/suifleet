// Generated protocol-value acceptance; not a production entry point.
#[test_only]
module sui_edge::protocol_value_tests {
    #[test]
    fun status_values_match_clients() {
        assert!(sui_edge::deployment_manager::status_applied() == 1, 0);
        assert!(sui_edge::deployment_manager::status_failed() == 2, 1);
    }
}
