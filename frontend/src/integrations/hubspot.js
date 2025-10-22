import { useState, useEffect } from "react";
import { Box, Button, CircularProgress, List, ListItem, ListItemText } from "@mui/material";
import axios from "axios";

export const HubspotIntegration = ({ user, org, integrationParams, setIntegrationParams }) => {
  const [isConnected, setIsConnected] = useState(false);
  const [isConnecting, setIsConnecting] = useState(false);

  useEffect(() => {
    setIsConnected(Boolean(integrationParams?.items || integrationParams?.credentials));
  }, [integrationParams?.items, integrationParams?.credentials]);

  const handleConnectClick = async () => {
    setIsConnecting(true);
    const popup = window.open("", "HubSpot Authorization", "width=600,height=700");
    if (!popup) {
      setIsConnecting(false);
      alert("Popup blocked. Please allow popups for this site.");
      return;
    }

    try {
      const resp = await axios.get("http://localhost:8000/integrations/hubspot/authorize", {
        params: { user_id: user, org_id: org },
      });
      const authorize_url = resp?.data?.authorize_url;
      if (!authorize_url) throw new Error("No authorize URL from server");

      popup.location.href = authorize_url;

      const pollTimer = window.setInterval(async () => {
        if (!popup || popup.closed) {
          window.clearInterval(pollTimer);
          await handleWindowClosed();
        }
      }, 500);
    } catch (e) {
      setIsConnecting(false);
      if (popup && !popup.closed) popup.close();
      alert(e?.response?.data?.detail || e?.message || "Authorization failed.");
    }
  };

  const handleWindowClosed = async () => {
    try {
      const resp = await axios.get("http://localhost:8000/integrations/hubspot/items", {
        params: { user_id: user, org_id: org },
      });
      const items = resp?.data || []; // Use full response array

      setIntegrationParams((prev) => ({ ...prev, items: items, type: "HubSpot" }));
      if (items.length > 0) setIsConnected(true);
    } catch (e) {
      alert(e?.response?.data?.detail || e?.message || "Failed to fetch HubSpot items.");
    } finally {
      setIsConnecting(false);
    }
  };

  return (
    <Box sx={{ mt: 2 }}>
      <Button
        variant="contained"
        onClick={isConnected ? () => {} : handleConnectClick}
        color={isConnected ? "success" : "primary"}
        disabled={isConnecting}
        sx={{ mb: 2 }}
      >
        {isConnected ? "HubSpot Connected" : isConnecting ? <CircularProgress size={20} /> : "Connect to HubSpot"}
      </Button>

      {integrationParams?.items && integrationParams.items.length > 0 && (
        <List>
          {integrationParams.items.map((item) => (
            <ListItem key={item.id} divider>
              <ListItemText
                primary={item.name}
                secondary={`Email: ${item.raw_properties?.email || "N/A"} | Created: ${item.creation_time}`}
              />
            </ListItem>
          ))}
        </List>
      )}
    </Box>
  );
};
