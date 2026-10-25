import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { FleetSelectionProvider, FleetScope, useFleetSelection } from "../../dapp/src/hooks/useFleetSelection";
import { FleetSwitcher } from "../../dapp/src/components/FleetSwitcher";
import { useDevices } from "../../dapp/src/hooks/useDevices";
import { control } from "./customer-wallet-browser";
Object.assign(window, { fleetTest: control });
function Inventory(){const q=useDevices();return <div data-testid="inventory">{q.isLoading?"Loading devices…":q.data?.map(d=><p key={d.objectId}>{d.name}</p>)}</div>;}
function App(){const s=useFleetSelection();return <><h1>SuiFleet</h1><FleetSwitcher/><button onClick={s.refresh}>Refresh fleets</button><FleetScope><h2>Devices in this fleet</h2><Inventory/><label>Draft note <input aria-label="Draft note"/></label></FleetScope></>;}
createRoot(document.getElementById("root")!).render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><FleetSelectionProvider><App/></FleetSelectionProvider></QueryClientProvider>);
