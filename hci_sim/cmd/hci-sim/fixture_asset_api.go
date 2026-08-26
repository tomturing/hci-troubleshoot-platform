package main

import (
	"encoding/json"
	"log"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"hci_sim/internal/controlplane"
	"hci_sim/internal/fixtureasset"
)

// registerControlPlaneAssetAPI 仅暴露修订式资产管理接口；浏览器不能直连 Runtime。
func registerControlPlaneAssetAPI(mux *http.ServeMux, controlToken string, allowInsecure bool, store fixtureasset.Store) {
	handler := func(w http.ResponseWriter, r *http.Request) {
		if !controlPlaneAuthorized(r, controlToken, allowInsecure) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		if store == nil {
			http.Error(w, "fixture asset registry unavailable", http.StatusServiceUnavailable)
			return
		}
		handleFixtureAssets(w, r, store)
	}
	mux.HandleFunc(controlPlaneFixtureAssetPrefix, handler)
	mux.HandleFunc(controlPlaneFixtureAssetPrefix+"/", handler)
}

func handleFixtureAssets(w http.ResponseWriter, r *http.Request, store fixtureasset.Store) {
	suffix := strings.Trim(strings.TrimPrefix(r.URL.Path, controlPlaneFixtureAssetPrefix), "/")
	if suffix == "" {
		switch r.Method {
		case http.MethodGet:
			assets, err := store.List(r.Context(), r.URL.Query().Get("signal_type"), r.URL.Query().Get("asset_type"), r.URL.Query().Get("status"))
			if err != nil {
				writeControlPlaneError(w, err)
				return
			}
			log.Printf("bundle_factory fixture_assets_list trace_id=%s count=%d", requestTraceID(r), len(assets))
			writeJSON(w, http.StatusOK, map[string]any{"assets": assets, "trace_id": requestTraceID(r)})
		case http.MethodPost:
			actor, err := requestActor(r, controlplane.RoleExpert)
			if err != nil {
				writeControlPlaneError(w, err)
				return
			}
			var request fixtureasset.CreateRequest
			decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 128*1024))
			decoder.DisallowUnknownFields()
			if err = decoder.Decode(&request); err != nil {
				http.Error(w, "asset request invalid", http.StatusBadRequest)
				return
			}
			asset, err := store.CreateRevision(r.Context(), request, actor.ID, requestTraceID(r))
			if err != nil {
				writeControlPlaneError(w, err)
				return
			}
			log.Printf("bundle_factory fixture_asset_revision_created trace_id=%s asset_key=%s revision=%d actor_id=%s", requestTraceID(r), asset.AssetKey, asset.Revision, actor.ID)
			writeJSON(w, http.StatusCreated, map[string]any{"asset": asset, "trace_id": requestTraceID(r)})
		default:
			http.NotFound(w, r)
		}
		return
	}
	parts := strings.Split(suffix, "/")
	assetKey, err := url.PathUnescape(parts[0])
	if err != nil || assetKey == "" {
		http.Error(w, "asset_key invalid", http.StatusBadRequest)
		return
	}
	if len(parts) == 1 && r.Method == http.MethodGet {
		assets, err := store.Get(r.Context(), assetKey)
		if err != nil {
			writeControlPlaneError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"assets": assets, "trace_id": requestTraceID(r)})
		return
	}
	if len(parts) != 3 || (parts[2] != "publish" && parts[2] != "retire") || r.Method != http.MethodPost {
		http.NotFound(w, r)
		return
	}
	revision, err := strconv.Atoi(parts[1])
	if err != nil || revision < 1 {
		http.Error(w, "revision invalid", http.StatusBadRequest)
		return
	}
	role := controlplane.RoleExpert
	if parts[2] == "publish" {
		role = controlplane.RolePublisher
	}
	actor, err := requestActor(r, role)
	if err != nil {
		writeControlPlaneError(w, err)
		return
	}
	var asset fixtureasset.Asset
	if parts[2] == "publish" {
		asset, err = store.Publish(r.Context(), assetKey, revision, actor.ID, requestTraceID(r))
	} else {
		asset, err = store.Retire(r.Context(), assetKey, revision, actor.ID, requestTraceID(r))
	}
	if err != nil {
		writeControlPlaneError(w, err)
		return
	}
	log.Printf("bundle_factory fixture_asset_%s trace_id=%s asset_key=%s revision=%d actor_id=%s", parts[2], requestTraceID(r), asset.AssetKey, asset.Revision, actor.ID)
	writeJSON(w, http.StatusOK, map[string]any{"asset": asset, "trace_id": requestTraceID(r)})
}
