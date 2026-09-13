package mqtt_test

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"time"

	pahomqtt "github.com/eclipse/paho.mqtt.golang"
	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	"github.com/sirupsen/logrus/hooks/test"
	"github.com/teslamotors/fleet-telemetry/datastore/mqtt"
	logrus "github.com/teslamotors/fleet-telemetry/logger"
	"github.com/teslamotors/fleet-telemetry/messages"
	"github.com/teslamotors/fleet-telemetry/metrics"
	"github.com/teslamotors/fleet-telemetry/protos"
	"github.com/teslamotors/fleet-telemetry/server/airbrake"
	"github.com/teslamotors/fleet-telemetry/telemetry"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

var _ = Describe("MQTT vehicle records", func() {
	const recordTopic = "test/topic/TEST123/records"
	const fieldTopic = "test/topic/TEST123/v/VehicleSpeed"
	var (
		config            *mqtt.Config
		producer          telemetry.Producer
		loggerHook        *test.Hook
		ackChan           chan *telemetry.Record
		originalNewClient func(*pahomqtt.ClientOptions) pahomqtt.Client
		serializer        *telemetry.BinarySerializer
		retainedByTopic   map[string]bool
		qosByTopic        map[string]byte
		failedTopic       string
		timeout           bool
	)

	BeforeEach(func() {
		resetPublishedTopics()
		retainedByTopic = make(map[string]bool)
		qosByTopic = make(map[string]byte)
		failedTopic = ""
		timeout = false
		originalNewClient = mqtt.PahoNewClient
		mqtt.PahoNewClient = func(_ *pahomqtt.ClientOptions) pahomqtt.Client {
			return &MockMQTTClient{
				PublishFunc: func(topic string, qos byte, retained bool, payload interface{}) pahomqtt.Token {
					publishedTopics[topic] = payload.([]byte)
					retainedByTopic[topic] = retained
					qosByTopic[topic] = qos
					return &MockToken{
						WaitTimeoutFunc: func(time.Duration) bool { return !(topic == failedTopic && timeout) },
						ErrorFunc: func() error {
							if topic == failedTopic {
								return errors.New("publish rejected")
							}
							return nil
						},
					}
				},
			}
		}
		var logger *logrus.Logger
		logger, loggerHook = logrus.NoOpLogger()
		config = &mqtt.Config{
			TopicBase:             "test/topic",
			QoS:                   1,
			Retained:              true,
			PublishVehicleRecords: true,
		}
		ackChan = make(chan *telemetry.Record, 1)
		var err error
		producer, err = mqtt.NewProducer(
			context.Background(), config, metrics.NewCollector(nil, logger),
			"test_namespace", airbrake.NewAirbrakeHandler(nil), ackChan,
			map[string]interface{}{"V": true}, logger,
		)
		Expect(err).NotTo(HaveOccurred())
		serializer = telemetry.NewBinarySerializer(
			&telemetry.RequestIdentity{DeviceID: "TEST123", SenderID: "vehicle_device.TEST123"},
			map[string][]telemetry.Producer{}, logger,
		)
	})

	AfterEach(func() {
		mqtt.PahoNewClient = originalNewClient
	})

	newRecord := func(payload *protos.Payload) *telemetry.Record {
		payloadBytes, err := proto.Marshal(payload)
		Expect(err).NotTo(HaveOccurred())
		message := messages.StreamMessage{
			TXID: []byte("1234"), SenderID: []byte("vehicle_device.TEST123"),
			MessageTopic: []byte("V"), Payload: payloadBytes,
		}
		messageBytes, err := message.ToBytes()
		Expect(err).NotTo(HaveOccurred())
		record, err := telemetry.NewRecord(serializer, messageBytes, "1", false)
		Expect(err).NotTo(HaveOccurred())
		return record
	}

	newSpeedRecord := func() *telemetry.Record {
		return newRecord(&protos.Payload{
			Data: []*protos.Datum{{
				Key:   protos.Field_VehicleSpeed,
				Value: &protos.Value{Value: &protos.Value_DoubleValue{DoubleValue: 0}},
			}},
		})
	}

	It("preserves sample precision, resend metadata and typed partial values without changing the shared record", func() {
		record := newRecord(&protos.Payload{
			Vin:       "UNTRUSTED",
			CreatedAt: timestamppb.New(time.Date(2026, 9, 11, 12, 34, 56, 123456789, time.UTC)),
			IsResend:  true,
			Data: []*protos.Datum{
				{Key: protos.Field_VehicleSpeed, Value: &protos.Value{Value: &protos.Value_DoubleValue{DoubleValue: 0}}},
				{Key: protos.Field_Location, Value: &protos.Value{Value: &protos.Value_LocationValue{LocationValue: &protos.LocationValue{Latitude: 0, Longitude: 0}}}},
				{Key: protos.Field_Locked, Value: &protos.Value{Value: &protos.Value_BooleanValue{BooleanValue: false}}},
				{Key: protos.Field_TimeToFullCharge, Value: &protos.Value{Value: &protos.Value_Invalid{Invalid: true}}},
				{Key: protos.Field_Odometer, Value: &protos.Value{Value: &protos.Value_LongValue{LongValue: 9007199254740993}}},
			},
		})
		before := proto.Clone(record.GetProtoMessage())
		producer.Produce(record)

		Expect(publishedTopics).To(HaveLen(6))
		Expect(publishedTopics[fieldTopic]).To(Equal([]byte("0")))
		Expect(retainedByTopic[fieldTopic]).To(BeTrue())
		Expect(retainedByTopic[recordTopic]).To(BeFalse())
		Expect(qosByTopic[recordTopic]).To(Equal(byte(1)))
		Expect(proto.Equal(record.GetProtoMessage(), before)).To(BeTrue())

		var decoded protos.Payload
		Expect(protojson.Unmarshal(publishedTopics[recordTopic], &decoded)).To(Succeed())
		Expect(proto.Equal(&decoded, before)).To(BeTrue())
		var envelope map[string]interface{}
		Expect(json.Unmarshal(publishedTopics[recordTopic], &envelope)).To(Succeed())
		Expect(envelope).To(HaveKeyWithValue("vin", "TEST123"))
		Expect(envelope).To(HaveKeyWithValue("created_at", "2026-09-11T12:34:56.123456789Z"))
		Expect(envelope).To(HaveKeyWithValue("is_resend", true))
		Expect(string(publishedTopics[recordTopic])).To(ContainSubstring(`"long_value":"9007199254740993"`))
		Expect(ackChan).To(Receive(Equal(record)))
	})

	It("does not invent a source timestamp or omit false resend metadata", func() {
		producer.Produce(newSpeedRecord())
		var envelope map[string]interface{}
		Expect(json.Unmarshal(publishedTopics[recordTopic], &envelope)).To(Succeed())
		Expect(envelope).To(HaveKeyWithValue("created_at", BeNil()))
		Expect(envelope).To(HaveKeyWithValue("is_resend", false))
	})

	It("keeps the existing field-only behavior when not enabled", func() {
		config.PublishVehicleRecords = false
		producer.Produce(newSpeedRecord())
		Expect(publishedTopics).To(HaveLen(1))
		Expect(publishedTopics).NotTo(HaveKey(recordTopic))
	})

	DescribeTable("withholds reliable acknowledgment if any publication fails",
		func(topic string, shouldTimeout bool) {
			failedTopic = topic
			timeout = shouldTimeout
			producer.Produce(newSpeedRecord())
			Expect(publishedTopics).To(HaveLen(2))
			Expect(ackChan).NotTo(Receive())
			Expect(loggerHook.LastEntry().Message).To(Equal("mqtt_publish_error"))
		},
		Entry("record rejected", recordTopic, false),
		Entry("record timeout", recordTopic, true),
		Entry("field rejected", fieldTopic, false),
	)

	It("withholds reliable acknowledgment if the record cannot be serialized", func() {
		record := newRecord(&protos.Payload{
			CreatedAt: &timestamppb.Timestamp{Seconds: 253402300800},
		})
		producer.Produce(record)
		Expect(publishedTopics).To(BeEmpty())
		Expect(ackChan).NotTo(Receive())
		Expect(loggerHook.LastEntry().Message).To(Equal("mqtt_process_payload_error"))
	})
	It("withholds publication and ACK if a required location archive write fails", func() {
		directory := filepath.Join(GinkgoT().TempDir(), "history")
		config.LocationArchive = &mqtt.LocationArchiveConfig{Directory: directory, Vehicles: map[string]string{"TEST123": "test_car"}}
		logger, _ := logrus.NoOpLogger()
		archivedProducer, err := mqtt.NewProducer(context.Background(), config, metrics.NewCollector(nil, logger), "test_namespace", airbrake.NewAirbrakeHandler(nil), ackChan, map[string]interface{}{"V": true}, logger)
		Expect(err).NotTo(HaveOccurred())
		Expect(os.Remove(directory)).To(Succeed())
		rec := newRecord(&protos.Payload{Data: []*protos.Datum{{Key: protos.Field_Location, Value: &protos.Value{Value: &protos.Value_LocationValue{LocationValue: &protos.LocationValue{Latitude: 0, Longitude: 0}}}}}})
		archivedProducer.Produce(rec)
		Expect(ackChan).NotTo(Receive())
		Expect(publishedTopics).To(BeEmpty())
		Expect(os.Mkdir(directory, 0700)).To(Succeed())
		archivedProducer.Produce(rec)
		Expect(ackChan).To(Receive(Equal(rec)))
		files, err := filepath.Glob(filepath.Join(directory, "*.ndjson"))
		Expect(err).NotTo(HaveOccurred())
		Expect(files).To(HaveLen(1))
		data, err := os.ReadFile(files[0])
		Expect(err).NotTo(HaveOccurred())
		Expect(string(data)).To(ContainSubstring("latitude"))
	})

})
