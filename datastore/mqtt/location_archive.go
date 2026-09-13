package mqtt

import (
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sync"
	"time"

	"github.com/teslamotors/fleet-telemetry/protos"
	"google.golang.org/protobuf/encoding/protojson"
)

// LocationArchiveConfig is opt-in and must point outside source/static web roots.
// Vehicle names are private runtime aliases keyed by authenticated vehicle ID.
type LocationArchiveConfig struct {
	Directory string            `json:"directory"`
	Vehicles  map[string]string `json:"vehicles"`
}

type locationArchive struct {
	directory string
	vehicles  map[string]string
	mu        sync.Mutex
}

func newLocationArchive(c *LocationArchiveConfig) (*locationArchive, error) {
	if c == nil {
		return nil, nil
	}
	invalid := errors.New("location archive configuration unavailable")
	if !filepath.IsAbs(c.Directory) || len(c.Vehicles) == 0 {
		return nil, invalid
	}
	aliases := make(map[string]bool)
	vehicles := make(map[string]string)
	for id, alias := range c.Vehicles {
		if id == "" || !regexp.MustCompile(`^[a-z0-9][a-z0-9_]{0,47}$`).MatchString(alias) || aliases[alias] {
			return nil, invalid
		}
		aliases[alias] = true
		vehicles[id] = alias
	}
	if err := os.MkdirAll(c.Directory, 0700); err != nil {
		return nil, invalid
	}
	info, err := os.Lstat(c.Directory)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, invalid
	}
	if err := os.Chmod(c.Directory, 0700); err != nil {
		return nil, invalid
	}
	return &locationArchive{directory: c.Directory, vehicles: vehicles}, nil
}

// append persists every Location datum before MQTT publication and vehicle ACK.
// Delayed/resend/invalid measurements are retained; retries may produce duplicates.
// Failures expose only a fixed category, never coordinates or authenticated IDs.
func (a *locationArchive) append(id string, payload *protos.Payload) error {
	if a == nil {
		return nil
	}
	alias, ok := a.vehicles[id]
	if !ok {
		return errors.New("location archive vehicle is not allowlisted")
	}
	received := time.Now().UTC()
	var source *string
	if stamp := payload.GetCreatedAt(); stamp != nil && stamp.CheckValid() == nil {
		value := stamp.AsTime().UTC().Format(time.RFC3339Nano)
		source = &value
	}
	var content []byte
	for _, datum := range payload.GetData() {
		if datum == nil || datum.GetKey() != protos.Field_Location {
			continue
		}
		raw := []byte("null")
		if datum.Value != nil {
			var err error
			raw, err = (protojson.MarshalOptions{UseProtoNames: true, EmitUnpopulated: true}).Marshal(datum.Value)
			if err != nil {
				return errors.New("location archive encoding failed")
			}
		}
		row := struct {
			Version  int             `json:"schema_version"`
			Vehicle  string          `json:"vehicle"`
			Source   *string         `json:"source_time"`
			Received string          `json:"received_at"`
			Resend   bool            `json:"is_resend"`
			Location json.RawMessage `json:"location"`
		}{1, alias, source, received.Format(time.RFC3339Nano), payload.GetIsResend(), raw}
		encoded, err := json.Marshal(row)
		if err != nil {
			return errors.New("location archive encoding failed")
		}
		content = append(content, encoded...)
		content = append(content, '\n')
	}
	if len(content) == 0 {
		return nil
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	name := filepath.Join(a.directory, received.Format("2006-01-02")+"_"+alias+".ndjson")
	if info, err := os.Lstat(name); err == nil && !info.Mode().IsRegular() {
		return errors.New("location archive file unavailable")
	}
	file, err := os.OpenFile(name, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return errors.New("location archive write failed")
	}
	defer file.Close()
	if err = file.Chmod(0600); err != nil {
		return errors.New("location archive write failed")
	}
	// Preserve a partial tail after an interrupted write, separating the next row.
	end, err := file.Seek(0, io.SeekEnd)
	if err == nil && end > 0 {
		tail := make([]byte, 1)
		_, err = file.ReadAt(tail, end-1)
		if err == nil && tail[0] != '\n' {
			content = append([]byte{'\n'}, content...)
		}
	}
	if err == nil {
		var count int
		count, err = file.Write(content)
		if err == nil && count != len(content) {
			err = io.ErrShortWrite
		}
	}
	if err == nil {
		err = file.Sync()
	}
	if err == nil {
		var directory *os.File
		directory, err = os.Open(a.directory)
		if err == nil {
			err = directory.Sync()
			directory.Close()
		}
	}
	if err != nil {
		return errors.New("location archive write failed")
	}
	return nil
}
