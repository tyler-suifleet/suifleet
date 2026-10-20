// Keep Linux kernel interfaces out of the Windows service binary.
#[cfg(not(windows))]
include!("linux_main.rs");

#[cfg(windows)]
fn main() -> anyhow::Result<()> {
    suifleet_windows::main()
}
