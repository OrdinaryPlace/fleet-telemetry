package mqtt

import (
	"encoding/json"
	"github.com/teslamotors/fleet-telemetry/protos"
	"google.golang.org/protobuf/types/known/timestamppb"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func locationPayload() *protos.Payload {
	return &protos.Payload{Vin: "UNTRUSTED_ID", CreatedAt: timestamppb.New(time.Date(2026, 1, 1, 0, 0, 0, 123456789, time.UTC)), IsResend: true, Data: []*protos.Datum{
		{Key: protos.Field_Location, Value: &protos.Value{Value: &protos.Value_LocationValue{LocationValue: &protos.LocationValue{Latitude: 0, Longitude: 0}}}},
		{Key: protos.Field_Location, Value: &protos.Value{Value: &protos.Value_Invalid{Invalid: true}}},
		{Key: protos.Field_VehicleSpeed, Value: &protos.Value{Value: &protos.Value_DoubleValue{DoubleValue: 1}}}}}
}
func TestLocationArchiveDurablePrivateConcurrentHistory(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "history")
	archive, err := newLocationArchive(&LocationArchiveConfig{directory, map[string]string{"AUTHENTICATED_ID": "test_car"}})
	if err != nil {
		t.Fatal(err)
	}
	var workers sync.WaitGroup
	for i := 0; i < 10; i++ {
		workers.Add(1)
		go func() {
			defer workers.Done()
			if err := archive.append("AUTHENTICATED_ID", locationPayload()); err != nil {
				t.Error(err)
			}
		}()
	}
	workers.Wait()
	files, _ := filepath.Glob(filepath.Join(directory, "*.ndjson"))
	if len(files) != 1 {
		t.Fatal("missing archive")
	}
	content, err := os.ReadFile(files[0])
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(content), "AUTHENTICATED_ID") || strings.Contains(string(content), "UNTRUSTED_ID") || strings.Contains(string(content), "VehicleSpeed") {
		t.Fatal("archive escaped location allowlist")
	}
	rows := strings.Split(strings.TrimSpace(string(content)), "\n")
	if len(rows) != 20 {
		t.Fatal("lost deliveries")
	}
	for _, line := range rows {
		var row map[string]interface{}
		if json.Unmarshal([]byte(line), &row) != nil || row["source_time"] != "2026-01-01T00:00:00.123456789Z" || row["is_resend"] != true {
			t.Fatal("lost source metadata")
		}
	}
	info, _ := os.Stat(files[0])
	if info.Mode().Perm() != 0600 {
		t.Fatal("archive permissions")
	}
	info, _ = os.Stat(directory)
	if info.Mode().Perm() != 0700 {
		t.Fatal("directory permissions")
	}
	// Restart appends without overwriting; incomplete tails remain recoverable.
	f, _ := os.OpenFile(files[0], os.O_APPEND|os.O_WRONLY, 0600)
	f.WriteString(`{"partial":`)
	f.Close()
	restarted, _ := newLocationArchive(&LocationArchiveConfig{directory, map[string]string{"AUTHENTICATED_ID": "test_car"}})
	if err := restarted.append("AUTHENTICATED_ID", locationPayload()); err != nil {
		t.Fatal(err)
	}
	content, _ = os.ReadFile(files[0])
	if !strings.Contains(string(content), "{\"partial\":\n{") {
		t.Fatal("partial tail was not preserved and separated")
	}
}
func TestLocationArchiveFailureAndAllowlist(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "history")
	a, err := newLocationArchive(&LocationArchiveConfig{directory, map[string]string{"AUTHENTICATED_ID": "test_car"}})
	if err != nil {
		t.Fatal(err)
	}
	if a.append("OTHER_ID", locationPayload()) == nil {
		t.Fatal("unlisted vehicle archived")
	}
	os.Remove(directory)
	if a.append("AUTHENTICATED_ID", locationPayload()) == nil {
		t.Fatal("lost write ignored")
	}
	if _, err := newLocationArchive(&LocationArchiveConfig{directory, map[string]string{"id": "../escape"}}); err == nil {
		t.Fatal("unsafe alias accepted")
	}
}
