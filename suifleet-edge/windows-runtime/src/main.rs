#[cfg(windows)]
fn main() -> anyhow::Result<()> {
    suifleet_windows::main()
}
#[cfg(not(windows))]
fn main() {
    eprintln!("The Windows service must run on Windows x86_64.");
    std::process::exit(1);
}
